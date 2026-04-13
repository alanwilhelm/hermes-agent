#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  tmux-send.sh --target <session:window.pane> [--message <text> | --file <path>]

Send a message to a live tmux pane by target name, with optional prompt
housekeeping and a post-send pane capture.

Options:
  -t, --target <target>       Tmux target, usually session:window.pane.
  -m, --message <text>        Message text to paste into the target pane.
  -f, --file <path>           Read the message body from a file.
      --codex-auto            Detect Codex pane state and choose queue vs submit.
      --interrupt             Force interrupt-and-submit now for Codex panes.
                              Sends Escape before submit.
      --tab                   Queue the pasted message with Tab instead of Enter.
      --queue                 Alias for --tab.
      --tab-only              Send Tab without pasting first. Use the same
                              --message/--file from the prior paste step when
                              you want verified two-step queueing.
      --queue-existing        Alias for --tab-only.
      --submit-only           Send Enter without pasting first. Use the same
                              --message/--file from the prior paste step when
                              you want verified two-step submit.
      --submit-existing       Alias for --submit-only.
      --submit                Alias for --interrupt.
      --escape                Send Escape before pasting the message.
      --clear-prompt          Send Ctrl-U before pasting the message.
      --no-enter              Paste only; do not press Enter.
      --paste-only            Alias for --no-enter.
      --submit-delay <secs>   Wait between paste and submit key. Default: 0.15
      --delay <seconds>       Wait before capture. Default: 0.2
      --capture-lines <n>     Lines to capture after sending. Default: 60
      --no-capture            Do not print a post-send capture.
      --no-verify-submit      Skip the post-submit acceptance check. Avoid
                              unless the pane is known to behave differently.
  -h, --help                  Show this help.

Notes:
  - If neither --message nor --file is provided, the script reads the message
    body from stdin.
  - The script pastes via the tmux buffer, then presses Enter by default.
  - Workspace standard for Codex cross-tmux sends is two-step:
      1. paste with --paste-only / --no-enter
      2. trigger with a separate helper call using the same --message/--file
  - Default non-completion trigger: --tab-only
  - Default completion-ping trigger: --submit-only
  - --codex-auto is retained for fallback automation, but it is not the
    standard cross-tmux Codex flow in this workspace.
  - Use --interrupt or --submit only for Codex panes when you explicitly want
    to interrupt the lane and submit immediately. The helper sends Escape
    first before pasting.
  - Any Enter-based send is verified after submit. If the message still appears
    to be sitting unsent in the compose box, the helper retries with
    Escape+Enter, then one repaste retry, and then fails closed.
  - Use --tab or --queue for Codex follow-up queueing after the message is
    pasted.
  - Use --escape only when you intentionally want to dismiss queueing or modal
    UI state before sending.
  - Use --clear-prompt when stale compose-box text needs to be removed first.
EOF
}

die() {
  printf 'tmux-send.sh: %s\n' "$1" >&2
  exit 1
}

target=""
message=""
message_file=""
send_tab=0
tab_only=0
submit_only=0
send_escape=0
clear_prompt=0
send_enter=1
codex_auto=0
interrupt_mode=0
submit_delay="0.15"
delay_seconds="0.2"
capture_lines="60"
show_capture=1
verify_submit=1
interrupt_settle_seconds="0.65"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--target)
      [[ $# -ge 2 ]] || die "missing value for $1"
      target="$2"
      shift 2
      ;;
    -m|--message)
      [[ $# -ge 2 ]] || die "missing value for $1"
      message="$2"
      shift 2
      ;;
    -f|--file)
      [[ $# -ge 2 ]] || die "missing value for $1"
      message_file="$2"
      shift 2
      ;;
    --codex-auto)
      codex_auto=1
      shift
      ;;
    --interrupt|--submit)
      interrupt_mode=1
      send_tab=0
      send_enter=1
      shift
      ;;
    --tab|--queue)
      send_tab=1
      send_enter=0
      shift
      ;;
    --tab-only|--queue-existing)
      send_tab=1
      send_enter=0
      tab_only=1
      shift
      ;;
    --submit-only|--submit-existing)
      send_tab=0
      send_enter=1
      submit_only=1
      shift
      ;;
    --escape)
      send_escape=1
      shift
      ;;
    --clear-prompt)
      clear_prompt=1
      shift
      ;;
    --no-enter)
      send_enter=0
      shift
      ;;
    --paste-only)
      send_enter=0
      shift
      ;;
    --submit-delay)
      [[ $# -ge 2 ]] || die "missing value for $1"
      submit_delay="$2"
      shift 2
      ;;
    --delay)
      [[ $# -ge 2 ]] || die "missing value for $1"
      delay_seconds="$2"
      shift 2
      ;;
    --capture-lines)
      [[ $# -ge 2 ]] || die "missing value for $1"
      capture_lines="$2"
      shift 2
      ;;
    --no-capture)
      show_capture=0
      shift
      ;;
    --no-verify-submit)
      verify_submit=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ -n "$target" ]] || die "--target is required"

if [[ -n "$message" && -n "$message_file" ]]; then
  die "use only one of --message or --file"
fi

if [[ -n "$message_file" ]]; then
  [[ -f "$message_file" ]] || die "message file not found: $message_file"
  message="$(cat "$message_file")"
elif [[ -z "$message" ]]; then
  if (( !tab_only )); then
    message="$(cat)"
  fi
fi

if (( !tab_only && !submit_only )); then
  [[ -n "$message" ]] || die "message body is empty"
elif (( verify_submit )) && [[ -z "$message" ]]; then
  die "--tab-only/--submit-only requires --message or --file when verification is enabled"
fi

capture_snapshot() {
  tmux capture-pane -pt "$target" -S "-$capture_lines"
}

message_still_queued() {
  local snapshot="$1"
  local tail_lines
  local message_line=""
  local after_lines=""
  tail_lines="$(printf '%s\n' "$snapshot" | tail -n 18)"

  if grep -Fq "Messages to be submitted after next tool call" <<<"$tail_lines"; then
    return 0
  fi

  if grep -Fq "Queued follow-up messages" <<<"$tail_lines"; then
    return 0
  fi

  message_line="$(
    printf '%s\n' "$tail_lines" |
      grep -n -F "› $message_probe" |
      tail -n 1 |
      cut -d: -f1
  )"

  if [[ -n "$message_line" ]]; then
    after_lines="$(printf '%s\n' "$tail_lines" | tail -n +"$((message_line + 1))")"

    if grep -Eq '^• ' <<<"$after_lines"; then
      return 1
    fi

    if grep -Eq 'Working \(' <<<"$after_lines"; then
      return 1
    fi

    return 0
  fi

  return 1
}

message_in_compose_box() {
  local snapshot="$1"
  local tail_lines
  tail_lines="$(printf '%s\n' "$snapshot" | tail -n 18)"

  [[ -n "$message_probe" ]] || return 1
  grep -Fq "› $message_probe" <<<"$tail_lines"
}

submission_accepted() {
  local snapshot="$1"

  if grep -Fq 'Selected model is at capacity' <<<"$snapshot"; then
    die "codex pane became blocked by model capacity during submit verification: $target"
  fi

  if message_still_queued "$snapshot"; then
    return 1
  fi

  return 0
}

verify_submission_clear() {
  local attempt=1
  local max_attempts=4
  local snapshot=""

  while (( attempt <= max_attempts )); do
    sleep "$delay_seconds"
    snapshot="$(capture_snapshot)"

    if submission_accepted "$snapshot"; then
      printf '%s' "$snapshot"
      return 0
    fi

    attempt=$(( attempt + 1 ))
  done

  printf '%s' "$snapshot"
  return 1
}

verify_paste_visible() {
  local attempt=1
  local max_attempts=4
  local snapshot=""

  while (( attempt <= max_attempts )); do
    sleep "$delay_seconds"
    snapshot="$(capture_snapshot)"

    if message_in_compose_box "$snapshot"; then
      printf '%s' "$snapshot"
      return 0
    fi

    attempt=$(( attempt + 1 ))
  done

  printf '%s' "$snapshot"
  return 1
}

verify_queue_accepted() {
  local attempt=1
  local max_attempts=4
  local snapshot=""

  while (( attempt <= max_attempts )); do
    sleep "$delay_seconds"
    snapshot="$(capture_snapshot)"

    if grep -Fq 'Selected model is at capacity' <<<"$snapshot"; then
      die "codex pane became blocked by model capacity during queue verification: $target"
    fi

    if ! message_in_compose_box "$snapshot"; then
      printf '%s' "$snapshot"
      return 0
    fi

    attempt=$(( attempt + 1 ))
  done

  printf '%s' "$snapshot"
  return 1
}

message_probe="$(
  printf '%s' "$message" |
    tr '\n' ' ' |
    sed 's/[[:space:]]\+/ /g' |
    cut -c1-80
)"

tmux display-message -p -t "$target" '#{session_name}:#{window_name}.#{pane_index}' >/dev/null \
  || die "tmux target not found: $target"

if (( codex_auto )); then
  pane_snapshot="$(tmux capture-pane -pt "$target" -S -80)"

  if grep -Fq 'Selected model is at capacity' <<<"$pane_snapshot"; then
    sleep 1
    pane_snapshot="$(tmux capture-pane -pt "$target" -S -80)"
    if grep -Fq 'Selected model is at capacity' <<<"$pane_snapshot"; then
      die "codex pane appears blocked by model capacity: $target"
    fi
  fi

  if (( !send_tab )) && grep -Eq 'Working \(|esc to interrupt' <<<"$pane_snapshot"; then
    send_tab=1
    send_enter=0
  fi
fi

if (( send_escape )); then
  tmux send-keys -t "$target" Escape
fi

if (( interrupt_mode )); then
  tmux send-keys -t "$target" Escape
  sleep "$interrupt_settle_seconds"
fi

if (( clear_prompt )); then
  tmux send-keys -t "$target" C-u
fi

if (( !tab_only && !submit_only )); then
  tmux set-buffer -- "$message"
  tmux paste-buffer -t "$target" -d
fi

if (( send_tab )); then
  sleep "$submit_delay"
  tmux send-keys -t "$target" Tab
elif (( send_enter )); then
  sleep "$submit_delay"
  tmux send-keys -t "$target" Enter
fi

sleep "$delay_seconds"

if (( verify_submit && !send_enter && !send_tab && !tab_only )); then
  post_paste_snapshot="$(verify_paste_visible || true)"
  if ! message_in_compose_box "$post_paste_snapshot"; then
    printf '%s\n' "$post_paste_snapshot"
    die "message did not appear in the compose box after paste-only send: $target"
  fi
fi

if (( verify_submit && send_tab )); then
  post_queue_snapshot="$(verify_queue_accepted || true)"

  if message_in_compose_box "$post_queue_snapshot"; then
    tmux send-keys -t "$target" Tab
    post_queue_snapshot="$(verify_queue_accepted || true)"
  fi

  if message_in_compose_box "$post_queue_snapshot"; then
    printf '%s\n' "$post_queue_snapshot"
    die "message still appears in the compose box after Tab queue verification and retry: $target"
  fi
fi

if (( verify_submit && send_enter )); then
  post_submit_snapshot="$(verify_submission_clear || true)"

  if message_still_queued "$post_submit_snapshot"; then
    tmux send-keys -t "$target" Escape
    sleep "$interrupt_settle_seconds"
    tmux send-keys -t "$target" Enter
    post_submit_snapshot="$(verify_submission_clear || true)"
  fi

  if message_still_queued "$post_submit_snapshot"; then
    tmux send-keys -t "$target" Escape
    sleep "$interrupt_settle_seconds"
    tmux send-keys -t "$target" C-u
    tmux set-buffer -- "$message"
    tmux paste-buffer -t "$target" -d
    sleep "0.35"
    tmux send-keys -t "$target" Enter
    post_submit_snapshot="$(verify_submission_clear || true)"
  fi

  if message_still_queued "$post_submit_snapshot"; then
    printf '%s\n' "$post_submit_snapshot"
    die "message still appears unsent after repeated interrupt/enter verification and repaste retry: $target"
  fi
fi

if (( show_capture )); then
  capture_snapshot
fi
