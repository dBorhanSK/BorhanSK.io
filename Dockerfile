FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DISPLAY=:1

# Debian 13 publishes the ARM64 Telegram Desktop package in trixie-backports.
RUN printf '%s\n' 'deb http://deb.debian.org/debian trixie-backports main' > /etc/apt/sources.list.d/trixie-backports.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl python3 python3-venv python3-pip \
    xvfb x11-utils xauth openbox x11vnc xdotool wmctrl xclip \
    websockify novnc supervisor \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-fas \
    fonts-noto-core fonts-noto-color-emoji fonts-dejavu-core \
    libgl1 libegl1 libglx-mesa0 libgl1-mesa-dri \
    libxkbcommon-x11-0 libxcb-cursor0 libxcb-xinerama0 libxcb-keysyms1 \
    libxcb-icccm4 libxcb-image0 libxcb-render-util0 libxcb-randr0 \
    libxcb-shape0 libxcb-xfixes0 libxcb-xkb1 libxcb-sync1 \
    dbus-x11 \
    && apt-get install -y --no-install-recommends -t trixie-backports telegram-desktop \
    && command -v telegram-desktop \
    && command -v websockify \
    && test -f /usr/share/novnc/vnc.html \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /bin/bash borhan \
    && mkdir -p /app/backend /data/telegram /data/logs /data \
    && chown -R borhan:borhan /app /data /home/borhan

WORKDIR /app/backend
COPY backend/requirements.txt /app/backend/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /app/backend/requirements.txt
COPY backend/ /app/backend/
COPY deploy/supervisord.conf /etc/supervisor/conf.d/borhan.conf
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh && chown -R borhan:borhan /app/backend

EXPOSE 8000 6080
ENTRYPOINT ["/entrypoint.sh"]
