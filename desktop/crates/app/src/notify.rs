//! Desktop notifications through `notify-send` (libnotify), started off the
//! UI thread; none when it is missing. Only while the window is not focused.

use std::process::{Command, Stdio};
use std::sync::OnceLock;

fn available() -> bool {
    static FOUND: OnceLock<bool> = OnceLock::new();
    *FOUND.get_or_init(|| {
        std::env::var_os("PATH").is_some_and(|p| std::env::split_paths(&p).any(|d| d.join("notify-send").is_file()))
    })
}

pub fn send(title: &str, body: &str, urgent: bool) {
    if !available() || std::env::var_os("LAZYAGENT_DESKTOP_NO_NOTIFY").is_some() {
        return;
    }
    let (title, body) = (title.to_string(), body.to_string());
    std::thread::spawn(move || {
        let _ = Command::new("notify-send")
            .args(["--app-name=lazyagent", "--icon=utilities-terminal"])
            .arg(format!("--urgency={}", if urgent { "critical" } else { "normal" }))
            .arg(title)
            .arg(body)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    });
}
