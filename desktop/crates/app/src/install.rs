//! Living outside a terminal: the window icon, `--install` (a `.desktop`
//! entry and its icon, so the app is in the application menu), the recent
//! repos, the picker shown when the app starts with no repo (from the menu
//! the cwd is `$HOME`), and the login-shell environment for agents started
//! from a menu launch (the session's environment lacks the shell profile's
//! PATH and friends).

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use eframe::egui::{self, RichText};
use lazyagent_proto::discovery::Env;

pub const LOGIN_ENV: &str = "LAZYAGENT_DESKTOP_LOGIN_ENV";
const RECENT_MAX: usize = 12;

/// The app's icon: a terminal prompt on the theme's dark surface.
pub fn icon_rgba(size: u32) -> Vec<u8> {
    let s = size as f32;
    let mut px = vec![0u8; (size * size * 4) as usize];
    let radius = s * 0.18;
    for y in 0..size {
        for x in 0..size {
            let (fx, fy) = (x as f32 + 0.5, y as f32 + 0.5);
            // Rounded square.
            let dx = (radius - fx).max(fx - (s - radius)).max(0.0);
            let dy = (radius - fy).max(fy - (s - radius)).max(0.0);
            let inside = (dx * dx + dy * dy).sqrt() <= radius;
            let mut c = if inside { [0x1e, 0x1e, 0x1e, 0xff] } else { [0, 0, 0, 0] };
            if inside {
                // ">" in the accent colour.
                let (u, v) = (fx / s, fy / s);
                let t = 0.07;
                let on_upper = (v - (0.30 + (u - 0.22) * 0.85)).abs() < t && (0.22..=0.47).contains(&u);
                let on_lower = (v - (0.72 - (u - 0.22) * 0.85)).abs() < t && (0.22..=0.47).contains(&u);
                if on_upper || on_lower {
                    c = [0xff, 0xa6, 0x2b, 0xff];
                }
                // "_" in green.
                if (0.52..=0.80).contains(&u) && (0.66..=0.76).contains(&v) {
                    c = [0x4e, 0xbf, 0x71, 0xff];
                }
            }
            let i = ((y * size + x) * 4) as usize;
            px[i..i + 4].copy_from_slice(&c);
        }
    }
    px
}

pub fn icon() -> egui::IconData {
    egui::IconData { rgba: icon_rgba(128), width: 128, height: 128 }
}

fn data_home(env: &Env) -> PathBuf {
    env.get("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env.get("HOME").unwrap_or_default()).join(".local/share"))
}

/// The `.desktop` entry's text.
pub fn desktop_entry(binary: &Path, python: Option<&str>, icon: &Path) -> String {
    let mut exec = format!("env {LOGIN_ENV}=1");
    if let Some(py) = python {
        exec.push_str(&format!(" LAZYAGENT_PYTHON={py}"));
    }
    exec.push_str(&format!(" {}", binary.display()));
    format!(
        "[Desktop Entry]\nType=Application\nName=lazyagent\nGenericName=Coding agents\n\
         Comment=Worktrees, coding agents and their terminals\nExec={exec}\nIcon={}\nTerminal=false\n\
         Categories=Development;\nStartupWMClass=lazyagent-desktop\n",
        icon.display()
    )
}

/// `--install`: the entry and its icon under `$XDG_DATA_HOME`.
pub fn install(env: &Env) -> Result<Vec<PathBuf>, String> {
    let binary = std::env::current_exe().map_err(|e| e.to_string())?;
    let data = data_home(env);
    let icon = data.join("icons/hicolor/128x128/apps/lazyagent-desktop.png");
    let entry = data.join("applications/lazyagent-desktop.desktop");
    for p in [&icon, &entry] {
        std::fs::create_dir_all(p.parent().unwrap()).map_err(|e| format!("{}: {e}", p.display()))?;
    }
    image::save_buffer(&icon, &icon_rgba(128), 128, 128, image::ExtendedColorType::Rgba8).map_err(|e| e.to_string())?;
    let python = env.get(lazyagent_proto::discovery::PYTHON_ENV);
    std::fs::write(&entry, desktop_entry(&binary, python.as_deref(), &icon)).map_err(|e| e.to_string())?;
    Ok(vec![entry, icon])
}

fn recent_path(env: &Env) -> PathBuf {
    crate::persist::path("", env).with_file_name("recent.json")
}

pub fn recent(env: &Env) -> Vec<String> {
    std::fs::read(recent_path(env))
        .ok()
        .and_then(|b| serde_json::from_slice::<Vec<String>>(&b).ok())
        .unwrap_or_default()
        .into_iter()
        .filter(|p| Path::new(p).is_dir())
        .collect()
}

pub fn remember(env: &Env, root: &str) {
    let mut list = recent(env);
    list.retain(|p| p != root);
    list.insert(0, root.to_string());
    list.truncate(RECENT_MAX);
    let path = recent_path(env);
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(path, serde_json::to_vec_pretty(&list).unwrap_or_default());
}

/// The login shell's environment (`$SHELL -lic 'env -0'`, 3 s at most),
/// for agents of a menu-launched app. `None` when it fails.
pub fn login_env(env: &Env) -> Option<Vec<(String, String)>> {
    let shell = env.get("SHELL").unwrap_or_else(|| "/bin/bash".into());
    const MARK: &str = "\u{0}LAZYAGENT-ENV\u{0}";
    let mut child = Command::new(&shell)
        .args(["-l", "-i", "-c", "printf '\\0LAZYAGENT-ENV\\0'; env -0"])
        .env_clear()
        .envs(env.vars())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let mut out = child.stdout.take()?;
    let reader = std::thread::spawn(move || {
        let mut buf = vec![];
        std::io::Read::read_to_end(&mut out, &mut buf).ok().map(|_| buf)
    });
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(10)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
    let bytes = reader.join().ok()??;
    let text = String::from_utf8_lossy(&bytes);
    // rc files may print things first: the marker says where env starts.
    let vars = text.split_once(MARK)?.1;
    let pairs: Vec<(String, String)> = vars
        .split('\0')
        .filter_map(|kv| kv.split_once('='))
        .filter(|(k, _)| !k.is_empty() && !matches!(*k, "SHLVL" | "PWD" | "OLDPWD" | "_"))
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
    (!pairs.is_empty()).then_some(pairs)
}

/// No repo to open: recent ones, or a path. Re-runs the app on the choice.
pub struct Picker {
    recent: Vec<String>,
    path: String,
    error: Option<String>,
    args: Vec<String>,
    focused: bool,
}

impl Picker {
    pub fn new(env: &Env, args: Vec<String>) -> Picker {
        Picker { recent: recent(env), path: String::new(), error: None, args, focused: false }
    }

    fn open(&mut self, path: &str) {
        match lazyagent_proto::discovery::repo_root(None, Some(Path::new(path))) {
            Ok(root) => {
                use std::os::unix::process::CommandExt;
                let exe = std::env::current_exe().unwrap_or_else(|_| "lazyagent-desktop".into());
                // Replaces this process: the window closes, the app opens on the repo.
                let err = Command::new(exe).arg(root).args(&self.args).exec();
                self.error = Some(format!("Could not start: {err}"));
            }
            Err(e) => self.error = Some(e.0),
        }
    }
}

impl eframe::App for Picker {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        egui::CentralPanel::default().show(ui, |ui| {
            ui.add_space(20.0);
            ui.heading("lazyagent");
            ui.label("Open a git repository:");
            ui.add_space(8.0);
            let mut chosen = None;
            for r in &self.recent {
                if ui.button(r).clicked() {
                    chosen = Some(r.clone());
                }
            }
            ui.add_space(8.0);
            ui.horizontal(|ui| {
                let edit =
                    ui.add(egui::TextEdit::singleline(&mut self.path).hint_text("/path/to/repo").desired_width(420.0));
                if !self.focused {
                    edit.request_focus();
                    self.focused = true;
                }
                let enter = edit.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
                if ui.button("Open").clicked() || enter {
                    chosen = Some(self.path.trim().to_string());
                }
            });
            if let Some(e) = &self.error {
                ui.add_space(8.0);
                ui.label(RichText::new(e).color(egui::Color32::from_rgb(0xe5, 0x53, 0x4b)));
            }
            if let Some(path) = chosen.filter(|p| !p.is_empty()) {
                let expanded = match path.strip_prefix("~/") {
                    Some(rest) => format!("{}/{rest}", std::env::var("HOME").unwrap_or_default()),
                    None => path,
                };
                self.open(&expanded);
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_icon_has_its_shapes() {
        let px = icon_rgba(128);
        let at = |x: u32, y: u32| &px[((y * 128 + x) * 4) as usize..((y * 128 + x) * 4 + 4) as usize];
        assert_eq!(at(0, 0)[3], 0, "transparent corner");
        assert_eq!(at(64, 20), [0x1e, 0x1e, 0x1e, 0xff]);
        assert_eq!(at(85, 91), [0x4e, 0xbf, 0x71, 0xff], "the cursor");
    }

    #[test]
    fn install_writes_an_entry_and_an_icon() {
        let dir = tempfile::tempdir().unwrap();
        let env = Env::process()
            .with("XDG_DATA_HOME", Some(dir.path().to_str().unwrap()))
            .with("LAZYAGENT_PYTHON", Some("/venv/bin/python"));
        let files = install(&env).unwrap();
        let entry = std::fs::read_to_string(&files[0]).unwrap();
        assert!(entry.contains("Exec=env LAZYAGENT_DESKTOP_LOGIN_ENV=1 LAZYAGENT_PYTHON=/venv/bin/python "), "{entry}");
        assert!(entry.contains("StartupWMClass=lazyagent-desktop"));
        assert!(files[1].ends_with("icons/hicolor/128x128/apps/lazyagent-desktop.png"));
        assert!(std::fs::metadata(&files[1]).unwrap().len() > 100);
    }

    #[test]
    fn recent_repos_most_recent_first() {
        let dir = tempfile::tempdir().unwrap();
        let env = Env::process().with("XDG_STATE_HOME", Some(dir.path().to_str().unwrap()));
        let (a, b) = (dir.path().join("a"), dir.path().join("b"));
        std::fs::create_dir_all(&a).unwrap();
        std::fs::create_dir_all(&b).unwrap();
        remember(&env, a.to_str().unwrap());
        remember(&env, b.to_str().unwrap());
        remember(&env, a.to_str().unwrap());
        remember(&env, "/no/longer/there");
        assert_eq!(recent(&env), [a.to_str().unwrap(), b.to_str().unwrap()]);
    }

    #[test]
    fn the_login_shell_environment() {
        let dir = tempfile::tempdir().unwrap();
        // As on Ubuntu: ~/.profile (read by a login shell) sources ~/.bashrc.
        std::fs::write(dir.path().join(".profile"), ". \"$HOME/.bashrc\"\n").unwrap();
        std::fs::write(dir.path().join(".bashrc"), "echo noise from the rc file\nexport FROM_PROFILE=yes\n").unwrap();
        // The test's HOME, so its rc files are the ones read.
        let env = Env::process()
            .with("HOME", Some(dir.path().to_str().unwrap()))
            .with("SHELL", Some("/bin/bash"))
            .with("FROM_PROFILE", None);
        let vars = login_env(&env).expect("bash -lic works here");
        assert!(vars.iter().any(|(k, v)| k == "FROM_PROFILE" && v == "yes"), "the rc file's export");
        assert!(vars.iter().any(|(k, _)| k == "PATH"));
    }
}
