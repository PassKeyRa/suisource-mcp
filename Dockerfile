FROM python:3.11-slim

# Install curl for downloading revela binary
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Determine architecture and download appropriate revela binary
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        REVELA_ARCH="x86_64-unknown-linux-gnu"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        REVELA_ARCH="aarch64-unknown-linux-gnu"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl -L -o /usr/local/bin/revela "https://github.com/verichains/revela/releases/download/v1.0.0/revela-${REVELA_ARCH}" && \
    chmod +x /usr/local/bin/revela

# Set working directory
WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Expose port (FastMCP default)
EXPOSE 8000

# Run the MCP server
CMD ["python", "server.py"]