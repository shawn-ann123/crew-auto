#!/usr/bin/env bash
# 安装用户级夜间自启动服务；无需 root。
set -euo pipefail

project_dir="$(cd "$(dirname "$0")" && pwd)"
user_unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$user_unit_dir"

sed "s|@@PROJECT_DIR@@|$project_dir|g" \
  "$project_dir/systemd/rm-research-nightly.service.in" \
  > "$user_unit_dir/rm-research-nightly.service"
install -m 0644 "$project_dir/systemd/rm-research-nightly.timer" \
  "$user_unit_dir/rm-research-nightly.timer"

systemctl --user daemon-reload
systemctl --user enable --now rm-research-nightly.timer
echo "夜间自启动已启用。程序每 15 分钟检查一次，仅在 .env 的夜间窗口和 DeepSeek 谷时内运行。"
systemctl --user list-timers rm-research-nightly.timer --no-pager
