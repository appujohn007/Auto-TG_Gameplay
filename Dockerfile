# Multi-runtime Dockerfile (Python 3.11 + Node.js 20)
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=8080

# Install Node.js, npm, curl, and essential build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirement files
COPY requirements.txt ./
COPY CATIZEN/package*.json ./CATIZEN/

# Install Python and Node dependencies
RUN pip install --no-cache-dir -r requirements.txt
RUN cd CATIZEN && npm install --omit=dev

# Copy the entire workspace into container
COPY . .

# Create persistent logs folder
RUN mkdir -p logs

# Expose Web Dashboard Port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD curl -f http://localhost:8080/healthz || exit 1

# Start the Auto-Player Supervisor
CMD ["python", "-u", "auto_player.py"]
