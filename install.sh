#!/usr/bin/env bash
# Instalador de Hefesto: comprueba Python >= 3.11 y enlaza `hefesto` en ~/.local/bin. Idempotente.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
bin_dir="${HEFESTO_BIN_DIR:-$HOME/.local/bin}"
home_dir="${HEFESTO_HOME:-$HOME/hefesto}"

py=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 \
    && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    py="$candidate"
    break
  fi
done
if [ -z "$py" ]; then
  echo "install.sh: se necesita Python >= 3.11 (python3 o python en el PATH)." >&2
  exit 3
fi
if ! command -v git >/dev/null 2>&1; then
  echo "install.sh: se necesita git." >&2
  exit 3
fi

mkdir -p "$bin_dir" "$home_dir"
chmod +x "$root/bin/hefesto"

case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*)
    # Windows nativo: un .cmd para PowerShell/cmd y un script para Git Bash, ambos apuntando a esta copia.
    win_root=$(cygpath -w "$root")
    printf '@echo off\r\ncall "%s\\bin\\hefesto.cmd" %%*\r\nexit /b %%ERRORLEVEL%%\r\n' "$win_root" > "$bin_dir/hefesto.cmd"
    printf '#!/usr/bin/env sh\nexec "%s/bin/hefesto" "$@"\n' "$root" > "$bin_dir/hefesto"
    chmod +x "$bin_dir/hefesto"
    ;;
  *)
    ln -sfn "$root/bin/hefesto" "$bin_dir/hefesto"
    ;;
esac

version=$(git -C "$root" describe --tags --exact-match 2>/dev/null \
  || "$py" -c 'import sys; sys.path.insert(0, sys.argv[1]); import hefesto; print(hefesto.__version__)' "$root")
printf '%s\n' "$version" > "$home_dir/VERSION"

echo "Hefesto $version instalado: $bin_dir/hefesto -> $root"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) echo "Aviso: $bin_dir no está en el PATH. Añádelo a tu perfil de shell (en Windows, a la variable PATH del usuario)." ;;
esac
