#!/bin/sh
exec python3 -m http.server 8903 --directory "$(dirname "$0")"
