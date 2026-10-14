#!/usr/bin/env bash
# Instalador de Hefesto: comprueba Python >= 3.11 y enlaza `hefesto` en ~/.local/bin. Idempotente.
set -euo pipefail

add_to_path=0
while [ $# -gt 0 ]; do
  case "$1" in
    --add-to-path)
      add_to_path=1
      shift
      ;;
    *)
      echo "install.sh: opción desconocida: $1" >&2
      exit 1
      ;;
  esac
done

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

if [ "$add_to_path" -eq 1 ]; then
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
      win_bin_dir=$(cygpath -w "$bin_dir")
      if [ "${HEFESTO_PATH_DRY_RUN:-0}" = "1" ]; then
        echo "[dry-run] añadiría $bin_dir ($win_bin_dir) al PATH de usuario en Windows (PowerShell)"
      else
        # shellcheck disable=SC2016
        ps_script='$target = $env:HEFESTO_ADD_PATH; if ($target) { $t = $target.TrimEnd("\"); $p = [Environment]::GetEnvironmentVariable("Path", "User"); if (-not $p) { $p = "" }; $parts = $p -split ";" | Where-Object { $_ -ne "" }; $exists = $false; foreach ($entry in $parts) { if ($entry.TrimEnd("\") -ieq $t) { $exists = $true; break } }; if (-not $exists) { if ($p) { $new = $p.TrimEnd(";") + ";" + $target } else { $new = $target }; [Environment]::SetEnvironmentVariable("Path", $new, "User") } }'
        export HEFESTO_ADD_PATH="$win_bin_dir"
        powershell.exe -NoProfile -Command "$ps_script"
        unset HEFESTO_ADD_PATH
      fi
      ;;
    *)
      profile="${HEFESTO_PROFILE:-$HOME/.profile}"
      profile_line="export PATH=\"$bin_dir:\$PATH\" # hefesto"
      if [ "${HEFESTO_PATH_DRY_RUN:-0}" = "1" ]; then
        echo "[dry-run] añadiría $bin_dir a $profile: $profile_line"
      else
        mkdir -p "$(dirname "$profile")"
        if [ -f "$profile" ] && grep -F -x -q "$profile_line" "$profile"; then
          :
        else
          if [ -s "$profile" ] && [ -n "$(tail -c 1 "$profile" 2>/dev/null)" ]; then
            printf '\n' >> "$profile"
          fi
          printf '%s\n' "$profile_line" >> "$profile"
        fi
      fi
      ;;
  esac
  echo "Abre una terminal nueva para que los cambios en el PATH surtan efecto."
else
  case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "Aviso: $bin_dir no está en el PATH. Añádelo a tu perfil de shell (en Windows, a la variable PATH del usuario)." ;;
  esac
fi
