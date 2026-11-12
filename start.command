#!/bin/bash
# Équivalent macOS de start.bat : double-clic dans le Finder, ou ./start.command dans le Terminal.
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
  python3 server.py
else
  echo "Python 3 est introuvable."
  echo "Installez-le avec : brew install python   (ou depuis https://www.python.org/downloads/macos/)"
fi

echo
read -n 1 -s -r -p "Appuyez sur une touche pour fermer..."
echo
