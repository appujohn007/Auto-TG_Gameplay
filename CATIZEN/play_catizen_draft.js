// play_catizen.js
// Autonomous Catizen WebSocket Automation Engine - NON-BLOCKING PIPELINE
// - Asynchronous Background Checks: Supportive queries (Airdrop, Speed Boost, Daily Check-In, Rank Rush)
//   do NOT block the main gameplay merge/store-buy loop!
// - Robust Fallback Timer: Automatically triggers next action if server is delayed.
// - Growth Multipliers (Features 2, 4, 5, Airdrops, 2x Speed, Deletion)
// - Unlimited Cat Merge Progression towards Level 14, 15, 20+

const WebSocket = require('ws');
global.window = global;
require('./pb_lib.js');
require('./pb_bundle.js');
const pb = global.window.pb;
const opcodes = require('./clean_opcodes.json');

const CmdToId = {};
const IdToCmd = opcodes;
for (const [id, name] of Object.entries(opcodes)) {
  CmdToId[name] = parseInt(id, 10);
}

let ws = null;
let transId = 1;
let currentGrid = [];
let userInfo = null;
let currentGold = 0;
let maxCatLvl = 13;
let heartbeatTimer = null;
let loopTimer = null;
let isActing = false;
let isReconnecting = false;

// Growth Timers
let lastGatherTime = 0;
let lastAirdropTime = 0;
let lastFreeCatTime = 0;
let lastBoostCheckTime = 0;
let lastCheckInTime = 0;
let lastRankRushTime = 0;
let lastRearrangeTime = 0;
let boostEndTime = 0;

const KNOWN_PRICES = {
  8: 3000000,
  9: 4500000
};

function sendPacket(cmdName, payloadObj) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;

  const cmdId = CmdToId[cmdName];
  if (!cmdId) return;

  const schema = pb[cmdName];
  if (!schema) return;

  const innerMsg = schema.create(payloadObj);
  const innerBytes = schema.encode(innerMsg).finish();

  const envelope = pb.CSMessage.create({
    cmdId: cmdId,
    transId: ++transId,
    body: innerBytes
  });
  const packetBuffer = pb.CSMessage.encode(envelope).finish();

  ws.send(packetBuffer);
}

async function getAuthSession() {
  console.log('1. Fetching Telegram WebApp initData...');
  let smData = null;
  for (let retry = 1; retry <= 3; retry++) {
    try {
      const smRes = await fetch('https://tg-game-sessionmaker.onrender.com/api/get-init-data', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bot: 'catizenbot', short_name: 'gameapp' })
      });
      smData = await smRes.json();
      if (smData && smData.initData) break;
    } catch (e) {
      console.log(`InitData fetch retry ${retry}/3...`);
      await new Promise(r => setTimeout(r, 2000));
    }
  }

  if (!smData || !smData.initData) {
    throw new Error('Failed to retrieve initData');
  }

  console.log('2. Authenticating via /pflogin...');
  const loginRes = await fetch('https://lg1.catizen.ai/pflogin', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      channelId: 1,
      bcId: 90001,
      data: {
        initdata: smData.initData,
        botname: 'catizenbot',
        inviter: 0,
        token: ''
      },
      language: 'en'
    })
  });

  const session = await loginRes.json();
  console.log(` Authenticated! User: ${session.nickName} (${session.name})`);
  return session;
}

function printGrid(cats) {
  console.log('----------------------------------------------------');
  console.log('  Catizen 12-Slot Board:');
  const fmt = (c, idx) => `[${String(idx).padStart(2, ' ')}: Lvl ${c || '-'}]`;
  console.log('  ' + cats.slice(0, 4).map((c, i) => fmt(c, i)).join(' '));
  console.log('  ' + cats.slice(4, 8).map((c, i) => fmt(c, i + 4)).join(' '));
  console.log('  ' + cats.slice(8, 12).map((c, i) => fmt(c, i + 8)).join(' '));
  console.log('----------------------------------------------------');
}

function getTopGoldShopLevel() {
  const serverMax = (userInfo && userInfo.exData && userInfo.exData.maxCatLvl) ? userInfo.exData.maxCatLvl : maxCatLvl;
  const boardMax = Math.max(...currentGrid, 0);
  const effectiveMax = Math.max(serverMax, boardMax);
  return Math.max(1, effectiveMax - 4);
}

async function connect() {
  try {
    const session = await getAuthSession();
    const wsUrl = session.addr.endsWith('/') ? session.addr : session.addr + '/';
    
    console.log(`3. Connecting to WebSocket Game Gateway: ${wsUrl}`);
    ws = new WebSocket(wsUrl, {
      headers: {
        'Host': 'gs1.catizen.ai:8203',
        'Origin': 'https://game.catizen.ai',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
      }
    });
    ws.binaryType = 'arraybuffer';

    ws.on('open', () => {
      console.log(' WebSocket Connected! Sending EnterGameReq (Cmd 13)...');
      isReconnecting = false;
      
      sendPacket('EnterGameReq', {
        accountId: Number(session.accountId),
        userId: Number(session.userId),
        name: String(session.name),
        token: String(session.token),
        time: Number(session.time),
        bcId: 90001,
        sex: 0,
        nickName: String(session.nickName),
        relogin: 0
      });

      clearInterval(heartbeatTimer);
      heartbeatTimer = setInterval(() => {
        sendPacket('HeartBeatReq', {});
      }, 5000);
    });

    ws.on('message', async (data) => {
      try {
        const buf = new Uint8Array(data);
        const envelope = pb.CSMessage.decode(buf);
        const cmdName = IdToCmd[envelope.cmdId] || `Op_${envelope.cmdId}`;

        if (cmdName === 'EnterGameAck' && pb.EnterGameAck) {
          const res = pb.EnterGameAck.decode(envelope.body);
          if (res.code === 0 && res.userInfo) {
            userInfo = res.userInfo;
            currentGrid = userInfo.cats.map(c => Number(c) || 0);
            currentGold = Number(userInfo.gold) || 0;
            boostEndTime = Number(userInfo.boostEndTime) || 0;
            if (userInfo.exData && userInfo.exData.maxCatLvl) {
              maxCatLvl = userInfo.exData.maxCatLvl;
            }
            const topShopLvl = getTopGoldShopLevel();
            console.log(`\n Logged in successfully! Welcome ${userInfo.name}!`);
            console.log(`💰 Gold: ${currentGold.toLocaleString()} | Highest Cat: Lvl ${Math.max(...currentGrid, maxCatLvl)} | Target Store Cat: Level ${topShopLvl}`);
            printGrid(currentGrid);
            scheduleNextAction(400);
          }
        }

        if (cmdName === 'MergeCatAck' && pb.MergeCatAck) {
          isActing = false;
          const res = pb.MergeCatAck.decode(envelope.body);
          if (res.cats && res.cats.length) {
            currentGrid = res.cats.map(c => Number(c) || 0);
            const topCat = Math.max(...currentGrid);
            if (topCat > maxCatLvl) {
              maxCatLvl = topCat;
              console.log(`\n🏆🏆 NEW RECORD UNLOCKED: LEVEL ${maxCatLvl} CAT CREATED! 🏆🏆`);
            } else {
              console.log(`\n MERGE SUCCESS! Highest Cat on Board: Level ${topCat}`);
            }
            printGrid(currentGrid);
            scheduleNextAction(300);
          }
        }

        if (cmdName === 'GenerateCatAck' && pb.GenerateCatAck) {
          isActing = false;
          const res = pb.GenerateCatAck.decode(envelope.body);
          if (res.catLvl !== undefined) {
            const spent = currentGold - Number(res.gold);
            currentGold = Number(res.gold) || currentGold;
            if (spent > 0) {
              KNOWN_PRICES[res.catLvl] = Math.round(spent * 1.12);
            }
            const targetSlot = (res.index !== undefined && res.index >= 0) ? res.index : currentGrid.findIndex(c => !c || c === 0);
            if (targetSlot !== -1) {
              currentGrid[targetSlot] = res.catLvl;
            }
            console.log(`\n TOP STORE CAT BOUGHT! Level ${res.catLvl} Cat at slot ${targetSlot}!`);
            console.log(`💰 Remaining Gold: ${currentGold.toLocaleString()}`);
            printGrid(currentGrid);
            scheduleNextAction(400);
          }
        }

        // Airdrop Ack (Async background event)
        if (cmdName === 'GetAirDropCatAck' && pb.GetAirDropCatAck) {
          const res = pb.GetAirDropCatAck.decode(envelope.body);
          if (res.cats && res.cats.length) {
            currentGrid = res.cats.map(c => Number(c) || 0);
            console.log(`\n🪂 CAT AIRDROP CLAIMED! Free cat landed on board!`);
            printGrid(currentGrid);
            scheduleNextAction(300);
          }
        }

        // Free Cat Ack
        if (cmdName === 'GetFreeCatAck' && pb.GetFreeCatAck) {
          const res = pb.GetFreeCatAck.decode(envelope.body);
          console.log(`\n🎁 FREE TIMER CAT RECEIVED! (Level ${res.catLvl || 1})`);
        }

        // Speed Booster Ack
        if (cmdName === 'BoostGoldAck' && pb.BoostGoldAck) {
          const res = pb.BoostGoldAck.decode(envelope.body);
          if (res.boostEndTime) {
            boostEndTime = Number(res.boostEndTime);
            console.log(`\n⚡ 2X SPEED BOOST ACTIVATED! (Until ${new Date(boostEndTime * 1000).toLocaleTimeString()})`);
          }
        }

        // Feature 2: Rank Rush Event Ack
        if (cmdName === 'RandomEventRankRushAck' && pb.RandomEventRankRushAck) {
          sendPacket('GetRandomEventRankRushAwardReq', { opType: 1 });
        }

        if (cmdName === 'GetRandomEventRankRushAwardAck' && pb.GetRandomEventRankRushAwardAck) {
          const res = pb.GetRandomEventRankRushAwardAck.decode(envelope.body);
          console.log(`\n🎉 RANK RUSH REWARD CLAIMED! FishCoin: ${res.fishCoin || '0'}`);
        }

        // Feature 5: Daily Check-In Ack
        if (cmdName === 'BCCheckInAck' && pb.BCCheckInAck) {
          console.log(`\n📅 DAILY CHECK-IN RECORDED ON SERVER!`);
        }

        // Feature 4: Cat Position Switch Ack
        if (cmdName === 'SwitchPosCatAck' && pb.SwitchPosCatAck) {
          isActing = false;
          const res = pb.SwitchPosCatAck.decode(envelope.body);
          if (res.cats && res.cats.length) {
            currentGrid = res.cats.map(c => Number(c) || 0);
            console.log(`\n📈 BOARD REARRANGED: Cats positioned optimally for earning efficiency!`);
            printGrid(currentGrid);
          }
          scheduleNextAction(300);
        }

        if (cmdName === 'DelCatAck' && pb.DelCatAck) {
          isActing = false;
          const res = pb.DelCatAck.decode(envelope.body);
          if (res.cats && res.cats.length) {
            currentGrid = res.cats.map(c => Number(c) || 0);
            console.log(`\n🗑️ OBSOLETE CAT DELETED! Freed up slot.`);
            printGrid(currentGrid);
            scheduleNextAction(300);
          }
        }

        if (cmdName === 'GatherGoldAck' && pb.GatherGoldAck) {
          const res = pb.GatherGoldAck.decode(envelope.body);
          if (res.gold) {
            currentGold = Number(res.gold) || currentGold;
            console.log(`💰 Idle Income Gathered! Gold: ${currentGold.toLocaleString()}`);
          }
          scheduleNextAction(400);
        }

        if (cmdName === 'ErrorAck' && pb.ErrorAck) {
          isActing = false;
          const res = pb.ErrorAck.decode(envelope.body);
          if (res.code === 15) {
            if (lastAttemptedLevel) {
              KNOWN_PRICES[lastAttemptedLevel] = Math.round(currentGold * 1.25);
            }
            scheduleNextAction(8000);
          } else {
            scheduleNextAction(2000);
          }
        }

      } catch (err) {
        console.error('Error handling packet:', err);
      }
    });

    ws.on('close', (code) => {
      console.log(`WebSocket closed: code=${code}. Reconnecting silently in 4 seconds...`);
      clearInterval(heartbeatTimer);
      clearTimeout(loopTimer);
      isActing = false;
      if (!isReconnecting) {
        isReconnecting = true;
        setTimeout(connect, 4000);
      }
    });

    ws.onerror = (err) => {
      console.error('WebSocket error:', err.message || err);
    };

  } catch (err) {
    console.error('Connection error:', err.message);
    setTimeout(connect, 5000);
  }
}

function scheduleNextAction(delayMs = 400) {
  clearTimeout(loopTimer);
  loopTimer = setTimeout(stepGameLoop, delayMs);
}

let lastAttemptedLevel = null;

function stepGameLoop() {
  if (isActing) return;

  const now = Date.now();
  const nowSec = Math.floor(now / 1000);
  const topShopLvl = getTopGoldShopLevel();
  const emptyCount = currentGrid.filter(c => !c || c === 0).length;

  // --- ASYNC SUPPORTIVE BACKGROUND CHECKS (DOES NOT BLOCK MERGING/STORE LOOP) ---
  if (now - lastCheckInTime > 14400000) {
    lastCheckInTime = now;
    console.log('📅 [DAILY CHECK-IN] Triggering daily streak check-in (Opcode 575)...');
    sendPacket('BCCheckInReq', { checkInType: 1 });
  }

  if (now - lastRankRushTime > 300000) {
    lastRankRushTime = now;
    console.log('🏆 [RANK RUSH] Checking for rank rush milestone awards (Opcode 63)...');
    sendPacket('RandomEventRankRushReq', {});
  }

  if (now - lastBoostCheckTime > 60000) {
    lastBoostCheckTime = now;
    if (boostEndTime <= nowSec) {
      console.log('⚡ [SPEED BOOST] Triggering free 2x speed booster...');
      sendPacket('BoostGoldReq', { Type: 1 });
    }
  }

  if (emptyCount >= 1 && (now - lastAirdropTime > 35000)) {
    lastAirdropTime = now;
    console.log('🪂 [AIRDROP CHECK] Checking for incoming cat airdrops...');
    sendPacket('GetAirDropCatReq', {});
  }

  if (emptyCount >= 1 && (now - lastFreeCatTime > 50000)) {
    lastFreeCatTime = now;
    console.log('🎁 [FREE CAT] Checking free recurring timer cat...');
    sendPacket('GetFreeCatReq', {});
  }

  // --- PRIMARY ACTION PIPELINE ---

  // 1. Priority One: Merge any identical cat pairs (merge higher levels first)
  let bestPair = null;

  for (let i = 0; i < currentGrid.length; i++) {
    const lvl1 = currentGrid[i];
    if (!lvl1 || lvl1 <= 0) continue;

    for (let j = i + 1; j < currentGrid.length; j++) {
      const lvl2 = currentGrid[j];
      if (lvl1 === lvl2) {
        if (!bestPair || lvl1 > bestPair.lvl) {
          bestPair = { i, j, lvl: lvl1 };
        }
      }
    }
  }

  if (bestPair) {
    isActing = true;
    console.log(`>>> [MERGE] Slot ${bestPair.i} (Lvl ${bestPair.lvl}) + Slot ${bestPair.j} (Lvl ${bestPair.lvl}) -> Lvl ${bestPair.lvl + 1}`);
    sendPacket('MergeCatReq', { indexs: [bestPair.i, bestPair.j] });
    return;
  }

  // 2. Priority Two: Intelligent Deletion of obsolete orphan cats if congested (<= 2 empty slots)
  if (emptyCount <= 2) {
    const threshold = Math.max(1, topShopLvl - 3);
    let lowestOrphan = null;

    for (let i = 0; i < currentGrid.length; i++) {
      const lvl = currentGrid[i];
      if (lvl > 0 && lvl <= threshold) {
        const count = currentGrid.filter(c => c === lvl).length;
        if (count === 1) {
          if (!lowestOrphan || lvl < lowestOrphan.lvl) {
            lowestOrphan = { index: i, lvl: lvl };
          }
        }
      }
    }

    if (lowestOrphan) {
      isActing = true;
      console.log(`>>> [SMART DELETE] Slot ${lowestOrphan.index} (Lvl ${lowestOrphan.lvl} <= ${threshold}) freed for high-tier cats...`);
      sendPacket('DelCatReq', { indexs: [lowestOrphan.index] });
      return;
    }
  }

  // 3. Priority Three: Collect idle income smoothly every 9 seconds
  if (now - lastGatherTime > 9000) {
    lastGatherTime = now;
    sendPacket('GatherGoldReq', {});
    return;
  }

  // 4. Priority Four: Buy Top Store Cat ONLY when gold balance is verified sufficient
  const emptyIndex = currentGrid.findIndex(c => !c || c === 0);
  if (emptyIndex !== -1) {
    let candidateLevel = topShopLvl;
    let expectedPrice = KNOWN_PRICES[candidateLevel] || 4500000;

    if (currentGold < expectedPrice && candidateLevel > 8) {
      candidateLevel = 8;
      expectedPrice = KNOWN_PRICES[8] || 3200000;
    }

    if (currentGold >= expectedPrice) {
      isActing = true;
      lastAttemptedLevel = candidateLevel;
      console.log(`>>> [VERIFIED STORE BUY] Gold ${currentGold.toLocaleString()} >= Required ${expectedPrice.toLocaleString()}. Purchasing Level ${candidateLevel} into Slot ${emptyIndex}...`);
      sendPacket('GenerateCatReq', { lvl: candidateLevel, Type: 1 });
      return;
    } else {
      const needed = expectedPrice - currentGold;
      console.log(`⏳ Accumulating gold: currently ${currentGold.toLocaleString()} / ${expectedPrice.toLocaleString()} (Need +${needed.toLocaleString()}). Sleeping...`);
      scheduleNextAction(5000);
      return;
    }
  }

  scheduleNextAction(4000);
}

connect();
