// The keyboard table the help sheet (?) is generated from: every TUI binding
// (app.py, worktree_list.py and the modals; `tui` holds their Textual key
// names, which a test checks against LazyAgent.BINDINGS) and the page's own
// keys. ui-plan.md §7a is the design.
//
// `where` says who acts on the key today: "tui" = the hosted TUI, when its
// tab has the keyboard; "page" = this page (in the TUI tab every key goes
// to the TUI).

export const SCOPES = {
  global: "Anywhere",
  sidebar: "Sidebar",
  outside: "Outside a terminal",
  modal: "In a dialog",
};

export const KEYMAP = [
  { keys: ["Ctrl+K"], tui: ["ctrl+k"], action: "Focus the sidebar", scope: "global", where: "page" },
  { keys: ["Ctrl+J"], tui: ["ctrl+j"], action: "Focus the active agent", scope: "global", where: "page" },
  { keys: ["Ctrl+D"], tui: ["ctrl+d"], action: "Show or hide the worktree's diff", scope: "global", where: "page" },
  { keys: ["Ctrl+L"], tui: ["ctrl+l"], action: "Open and focus the worktree shell", scope: "global", where: "page" },
  {
    keys: ["Alt+]", "Alt+N"],
    tui: ["alt+right_square_bracket", "alt+n"],
    action: "Next agent tab",
    scope: "global",
    where: "page",
  },
  {
    keys: ["Alt+[", "Alt+P"],
    tui: ["alt+left_square_bracket", "alt+p"],
    action: "Previous agent tab",
    scope: "global",
    where: "page",
  },
  { keys: ["Alt+G"], tui: ["alt+g"], action: "Refresh git status", scope: "global", where: "page" },
  { keys: ["J", "K"], tui: ["j", "k"], action: "Next / previous worktree (also ↓ / ↑; Enter: its agent)", scope: "sidebar", where: "page" },
  { keys: ["S"], tui: ["s"], action: "Spawn an agent", scope: "outside", where: "page" },
  { keys: ["X"], tui: ["x"], action: "Stop the agent", scope: "outside", where: "page" },
  { keys: ["C"], tui: ["c"], action: "Create a worktree (and launch an agent)", scope: "outside", where: "page" },
  { keys: ["D"], tui: ["d"], action: "Remove the worktree", scope: "outside", where: "page" },
  { keys: ["R"], tui: ["r"], action: "Refresh the worktree list", scope: "outside", where: "page" },
  { keys: ["?"], tui: ["question_mark"], action: "This help (the TUI has its own)", scope: "outside", where: "page" },
  {
    keys: ["Q"],
    tui: ["q"],
    action: "Quit lazyagent: no key in the page (it stops every agent); use q in the TUI",
    scope: "outside",
    where: "tui",
  },
  {
    keys: ["N", "D", "R", "L", "Backspace"],
    tui: ["n", "d", "r", "l", "backspace"],
    action: "Spawn dialog: normal, skip permissions, resume (pick / last), new session (Backspace: TUI only)",
    scope: "modal",
    where: "page",
  },
  { keys: ["Y", "N"], tui: ["y", "n"], action: "Confirm dialog: yes / no", scope: "modal", where: "page" },
  { keys: ["F"], tui: ["f"], action: "Remove dialog: toggle force", scope: "modal", where: "page" },
  { keys: ["Esc"], tui: ["escape"], action: "Close a dialog (this help too)", scope: "modal", where: "page" },
  { keys: ["Ctrl+Enter"], tui: [], action: "Send: the reply box, a dialog's instruction, a diff comment", scope: "modal", where: "page" },
  { keys: ["G", "A"], tui: [], action: "The attention queue (g then a)", scope: "outside", where: "page" },
  { keys: ["G", "W"], tui: [], action: "The worktree view (g then w)", scope: "outside", where: "page" },
  { keys: ["J", "K", "Enter"], tui: [], action: "Attention queue: next / previous item, Enter: its terminal", scope: "outside", where: "page" },
  { keys: ["Ctrl+."], tui: [], action: "The PR / CI panel of the worktree", scope: "global", where: "page" },
];
