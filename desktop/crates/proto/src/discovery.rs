//! Finding a repo's engine and starting one (engine-split §4f, §6d).
//!
//! The rules are the Python ones (`engine/runtime.py`, `launcher.find`):
//! the runtime dir is named after a hash of the exact repo root string, and
//! `engine.json` there names the socket. Starting an engine is delegated to
//! `lazyagent engine start`, which owns the lock, the log and the races.

use std::io::Read;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

pub const SOCKET_ENV: &str = "LAZYAGENT_ENGINE_SOCKET";
pub const AUTOSTART_ENV: &str = "LAZYAGENT_ENGINE_AUTOSTART";
pub const PYTHON_ENV: &str = "LAZYAGENT_PYTHON";
/// Variables of a client that mean nothing to an engine (launcher.py).
const CLIENT_ONLY_ENV: &[&str] = &["LAZYAGENT_ENGINE", "LAZYAGENT_ENGINE_SOCKET", "LAZYAGENT_HOST"];
pub const START_TIMEOUT: Duration = Duration::from_secs(20);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DiscoveryError(pub String);

impl std::fmt::Display for DiscoveryError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for DiscoveryError {}

/// The repo root an engine is named after, as the engine computes it: the
/// given path resolved, else `git rev-parse --show-toplevel` from the cwd.
pub fn repo_root(path: Option<&Path>, cwd: Option<&Path>) -> Result<String, DiscoveryError> {
    if let Some(p) = path {
        let resolved = std::fs::canonicalize(p).map_err(|e| DiscoveryError(format!("{}: {e}", p.display())))?;
        return Ok(resolved.to_string_lossy().into_owned());
    }
    let mut git = Command::new("git");
    if let Some(dir) = cwd {
        git.current_dir(dir);
    }
    let out = git
        .args(["rev-parse", "--show-toplevel"])
        .stderr(Stdio::null())
        .output()
        .map_err(|e| DiscoveryError(format!("cannot run git: {e}")))?;
    if !out.status.success() {
        let cwd = cwd.map(Path::to_path_buf).or_else(|| std::env::current_dir().ok());
        let cwd = cwd.map(|p| p.display().to_string()).unwrap_or_default();
        return Err(DiscoveryError(format!("Not inside a git repository: {cwd}")));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim_end_matches(['\n', '\r']).to_string())
}

pub fn repo_digest(root: &str) -> String {
    sha1_smol::Sha1::from(root.as_bytes()).digest().to_string()[..12].to_string()
}

/// The environment discovery reads and a started engine inherits: the
/// process's, with overrides (tests run isolated engines side by side).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Env {
    overrides: Vec<(String, Option<String>)>,
}

impl Env {
    /// The process environment as it is.
    pub fn process() -> Env {
        Env::default()
    }

    /// Sets (or, with `None`, unsets) a variable.
    pub fn with(mut self, name: &str, value: Option<&str>) -> Env {
        self.overrides.retain(|(n, _)| n != name);
        self.overrides.push((name.to_string(), value.map(str::to_string)));
        self
    }

    /// A variable's value; empty counts as unset.
    pub fn get(&self, name: &str) -> Option<String> {
        match self.overrides.iter().find(|(n, _)| n == name) {
            Some((_, v)) => v.clone(),
            None => std::env::var(name).ok(),
        }
        .filter(|v| !v.is_empty())
    }

    /// The full environment, overrides applied.
    pub fn vars(&self) -> Vec<(String, String)> {
        let mut vars: Vec<(String, String)> =
            std::env::vars().filter(|(n, _)| !self.overrides.iter().any(|(o, _)| o == n)).collect();
        vars.extend(self.overrides.iter().filter_map(|(n, v)| v.clone().map(|v| (n.clone(), v))));
        vars
    }

    fn apply(&self, cmd: &mut Command) {
        for (n, v) in &self.overrides {
            match v {
                Some(v) => cmd.env(n, v),
                None => cmd.env_remove(n),
            };
        }
    }
}

/// `$XDG_RUNTIME_DIR/lazyagent/<hash>`, else `<tmp>/lazyagent-<uid>/<hash>`.
pub fn runtime_dir_with(root: &str, get: impl Fn(&str) -> Option<String>) -> PathBuf {
    let digest = repo_digest(root);
    if let Some(base) = get("XDG_RUNTIME_DIR") {
        return Path::new(&base).join("lazyagent").join(digest);
    }
    // Python's tempfile.gettempdir(): TMPDIR, TEMP, TMP, then /tmp.
    let tmp = ["TMPDIR", "TEMP", "TMP"].iter().find_map(|n| get(n)).unwrap_or_else(|| "/tmp".into());
    // SAFETY: getuid has no preconditions and cannot fail.
    let uid = unsafe { libc::getuid() };
    Path::new(&tmp).join(format!("lazyagent-{uid}")).join(digest)
}

pub fn runtime_dir(root: &str, env: &Env) -> PathBuf {
    runtime_dir_with(root, |n| env.get(n))
}

/// `engine.json` when it names this repo.
pub fn engine_info(root: &str, env: &Env) -> Option<serde_json::Value> {
    let text = std::fs::read_to_string(runtime_dir(root, env).join("engine.json")).ok()?;
    let info: serde_json::Value = serde_json::from_str(&text).ok()?;
    (info.get("repo_root")?.as_str()? == root).then_some(info)
}

/// Whether a process accepts connections on the socket.
pub fn alive(path: &Path) -> bool {
    UnixStream::connect(path).is_ok()
}

/// The socket of a live engine serving `root`, or `None`.
pub fn find(root: &str, env: &Env) -> Option<PathBuf> {
    let info = engine_info(root, env)?;
    let path = PathBuf::from(info.get("socket")?.as_str()?);
    alive(&path).then_some(path)
}

/// How to run `lazyagent engine start`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Starter {
    /// `<python> -m lazyagent engine start`
    Python(PathBuf),
    /// `<lazyagent> engine start`
    Command(PathBuf),
}

impl Starter {
    /// `LAZYAGENT_PYTHON` (set by `lazyagent desktop`), else `lazyagent` on PATH.
    pub fn from_env(env: &Env) -> Option<Starter> {
        if let Some(py) = env.get(PYTHON_ENV) {
            return Some(Starter::Python(py.into()));
        }
        which("lazyagent", env).map(Starter::Command)
    }

    /// `<python> -m lazyagent` or `<lazyagent>`, to add a subcommand to.
    pub fn command(&self) -> Command {
        match self {
            Starter::Python(py) => {
                let mut c = Command::new(py);
                c.args(["-m", "lazyagent"]);
                c
            }
            Starter::Command(bin) => Command::new(bin),
        }
    }

    pub fn describe(&self) -> String {
        match self {
            Starter::Python(py) => format!("{} -m lazyagent", py.display()),
            Starter::Command(bin) => bin.display().to_string(),
        }
    }
}

pub fn which(name: &str, env: &Env) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    let path = env.get("PATH")?;
    std::env::split_paths(&path)
        .map(|dir| dir.join(name))
        .find(|p| p.metadata().map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false))
}

/// Runs `lazyagent engine start <root>` (idempotent: it returns at once
/// when an engine already serves the repo) and returns the socket.
pub fn start_engine(root: &str, starter: &Starter, env: &Env, timeout: Duration) -> Result<PathBuf, DiscoveryError> {
    let mut cmd = starter.command();
    env.apply(&mut cmd);
    cmd.args(["engine", "start", root]).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    for name in CLIENT_ONLY_ENV {
        cmd.env_remove(name);
    }
    let mut child = cmd.spawn().map_err(|e| DiscoveryError(format!("cannot run {}: {e}", starter.describe())))?;
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(5)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(DiscoveryError(format!(
                    "`{} engine start` did not finish in {} s",
                    starter.describe(),
                    timeout.as_secs()
                )));
            }
            Err(e) => return Err(DiscoveryError(format!("waiting for the engine start: {e}"))),
        }
    };
    let mut output = String::new();
    if let Some(mut out) = child.stdout.take() {
        let _ = out.read_to_string(&mut output);
    }
    if let Some(mut err) = child.stderr.take() {
        let _ = err.read_to_string(&mut output);
    }
    if !status.success() {
        return Err(DiscoveryError(format!("the engine did not start ({status}): {}", output.trim())));
    }
    find(root, env)
        .ok_or_else(|| DiscoveryError(format!("the engine started but its socket is not there: {}", output.trim())))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Located {
    pub socket: PathBuf,
    /// The socket came from `LAZYAGENT_ENGINE_SOCKET`.
    pub from_env: bool,
}

/// The engine socket for `root`: `LAZYAGENT_ENGINE_SOCKET`, else a live
/// engine, else none.
pub fn locate(root: &str, env: &Env) -> Option<Located> {
    if let Some(s) = env.get(SOCKET_ENV) {
        return Some(Located { socket: s.into(), from_env: true });
    }
    find(root, env).map(|socket| Located { socket, from_env: false })
}

/// Auto-start applies unless a socket is forced or it is turned off.
pub fn autostart_enabled(env: &Env) -> bool {
    env.get(SOCKET_ENV).is_none() && env.get(AUTOSTART_ENV).as_deref() != Some("0")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn digest_matches_python() {
        // hashlib.sha1(b"/tmp/repo").hexdigest()[:12]
        assert_eq!(repo_digest("/tmp/repo"), "d00fead8e35b");
    }

    #[test]
    fn runtime_dir_rules() {
        let with_xdg = |n: &str| (n == "XDG_RUNTIME_DIR").then(|| "/run/user/7".to_string());
        assert_eq!(runtime_dir_with("/tmp/repo", with_xdg), PathBuf::from("/run/user/7/lazyagent/d00fead8e35b"));
        let tmp = |n: &str| (n == "TMPDIR").then(|| "/var/tmp".to_string());
        let dir = runtime_dir_with("/tmp/repo", tmp);
        assert!(dir.starts_with("/var/tmp"), "{dir:?}");
        assert!(dir.parent().unwrap().file_name().unwrap().to_string_lossy().starts_with("lazyagent-"));
    }
}
