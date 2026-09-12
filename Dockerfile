FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8501

WORKDIR /app

# Install system utilities and Xvfb (Virtual Framebuffer for headless GUI support)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's Chromium browser and its system dependencies
RUN playwright install --with-deps chromium

# Copy application source code
COPY . .

# Expose default port
EXPOSE 8501

# Run Streamlit inside xvfb-run virtual display server so Playwright never crashes from missing XServer
CMD ["sh", "-c", "xvfb-run --auto-servernum --server-args='-screen 0 1280x1024x24' streamlit run app.py --server.port=${PORT:-8501} --server.address=0.0.0.0 --server.headless=true"]
