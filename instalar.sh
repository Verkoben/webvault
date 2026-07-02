#!/usr/bin/env bash
set -e

sudo apt update

sudo apt install -y \
  python3 \
  python3-pip \
  python3-dev \
  sqlite3 \
  libsqlite3-dev \
  libxml2-dev \
  libxslt1-dev \
  libjpeg-dev \
  zlib1g-dev \
  libpng-dev \
  libwebp-dev \
  build-essential \
  git

python3 -m pip install -U pip --break-system-packages

python3 -m pip install -U \
  Pillow \
  tqdm \
  lxml \
  ebooklib \
  beautifulsoup4 \
  requests \
  markdown \
  python-dateutil \
  chardet \
  --break-system-packages

python3 - <<'EOF'
mods = ["PIL","tqdm","lxml","ebooklib","bs4","requests","markdown","dateutil","chardet"]
for m in mods:
    __import__(m)
    print("[OK]", m)
EOF

echo "WebVault listo."
