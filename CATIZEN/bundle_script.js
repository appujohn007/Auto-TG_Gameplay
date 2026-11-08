const fs = require('fs');

const pbLib = fs.readFileSync('pb_lib.js', 'utf8');
const pbBundle = fs.readFileSync('pb_bundle.js', 'utf8');
const opcodes = fs.readFileSync('clean_opcodes.json', 'utf8');
let mainCode = fs.readFileSync('play_catizen.js', 'utf8');

// Replace local requires in main code
mainCode = mainCode.replace("const WebSocket = require('ws');\r\n", "");
mainCode = mainCode.replace("const WebSocket = require('ws');\n", "");
mainCode = mainCode.replace("global.window = global;\r\n", "");
mainCode = mainCode.replace("global.window = global;\n", "");
mainCode = mainCode.replace("require('./pb_lib.js');\r\n", "");
mainCode = mainCode.replace("require('./pb_lib.js');\n", "");
mainCode = mainCode.replace("require('./pb_bundle.js');\r\n", "");
mainCode = mainCode.replace("require('./pb_bundle.js');\n", "");
mainCode = mainCode.replace(
  "const opcodes = require('./clean_opcodes.json');",
  "const opcodes = " + opcodes.trim() + ";"
);

const fullBundle = `// play_catizen_standalone.js
// Autonomous Catizen WebSocket Automation Engine (Standalone Bundle)
// Contains inlined Protobuf library, compiled schemas, opcode mappings, and engine loop.

const WebSocket = require('ws');
global.window = global;

// ============================================================================
// SECTION 1: PROTOBUF RUNTIME (pb_lib)
// ============================================================================
${pbLib}

// ============================================================================
// SECTION 2: PROTOBUF SCHEMAS (pb_bundle)
// ============================================================================
${pbBundle}

// ============================================================================
// SECTION 3: GAMEPLAY ENGINE & OPCODES
// ============================================================================
${mainCode}
`;

fs.writeFileSync('play_catizen_standalone.js', fullBundle, 'utf8');
console.log('Successfully written play_catizen_standalone.js');
