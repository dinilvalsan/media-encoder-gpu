# Use NVIDIA CUDA runtime image (not base)
FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    software-properties-common \
    && add-apt-repository ppa:ubuntuhandbook1/ffmpeg7 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
    ffmpeg \
    python3.11 \
    python3-pip \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy handler script
COPY handler.py .

# Verify installations (with better error handling)
RUN echo "=== Verifying FFmpeg ===" && \
    ffmpeg -version && \
    echo "\n=== Checking available encoders ===" && \
    ffmpeg -hide_banner -encoders 2>/dev/null | grep -E "h264|hevc|nvenc" || echo "NVENC not found - will use CPU" && \
    echo "\n=== Python version ===" && \
    python3 --version

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import runpod; print('OK')" || exit 1

# Run the handler
CMD ["python3", "-u", "handler.py"]