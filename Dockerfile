# Use NVIDIA CUDA base image for GPU support
FROM nvidia/cuda:12.1.0-base-ubuntu22.04

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install FFmpeg with NVENC support and Python
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    python3.11 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy handler script
COPY handler.py .

# Verify installations
RUN echo "=== Verifying FFmpeg ===" && \
    ffmpeg -version && \
    echo "\n=== Checking NVENC support ===" && \
    ffmpeg -hide_banner -encoders 2>/dev/null | grep nvenc && \
    echo "\n=== Python version ===" && \
    python3 --version

# Health check (optional)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import runpod; print('OK')" || exit 1

# Run the handler
CMD ["python3", "-u", "handler.py"]