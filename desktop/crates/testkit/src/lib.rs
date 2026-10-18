//! A scratch world for tests against the real Python engine: a git repo, a
//! `claude` shim on PATH that runs `tests/engine/fake_agent.py`, and an
//! isolated runtime/state dir, so engines of parallel tests never meet.
//!
//! Python comes from `LAZYAGENT_PYTHON`, else the repo's `.venv`. Without
//! one that imports lazyagent the test is skipped with a message, unless
//! `LAZYAGENT_REQUIRE_ENGINE=1` (CI), which makes it fail instead.
//!
//! Every engine a world starts runs in its own process group, and `Drop`
//! kills the groups (engine, agents, shells), so nothing outlives a test.

use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::sync::mpsc::Receiver;
use std::time::{Duration, Instant};

use lazyagent_proto::client::{Client, ClientEvent, StreamSink};
use lazyagent_proto::discovery::{self, Env};
use lazyagent_proto::types::{HelloResult, StreamClosed, StreamGeometry};

pub const TIMEOUT: Duration = Duration::from_secs(15);

/// The lazyagent source tree (this crate lives in `desktop/crates/testkit`).
pub fn source_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..").canonicalize().unwrap()
}

/// A Python that can import lazyagent, or why there is none.
pub fn python() -> Result<PathBuf, String> {
    let py = std::env::var_os("LAZYAGENT_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| source_root().join(".venv/bin/python"));
    let ok = Command::new(&py)
        .args(["-c", "import lazyagent.engine"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    if ok { Ok(py) } else { Err(format!("{} cannot import lazyagent", py.display())) }
}

pub struct World {
    pub tmp: tempfile::TempDir,
    pub repo: String,
    pub env: Env,
    pub python: PathBuf,
    engines: Vec<Child>,
}

impl World {
    /// A new world, or `None` (test skipped) when no Python engine is available.
    pub fn new() -> Option<World> {
        let python = match python() {
            Ok(p) => p,
            Err(why) if std::env::var("LAZYAGENT_REQUIRE_ENGINE").as_deref() == Ok("1") => {
                panic!("LAZYAGENT_REQUIRE_ENGINE=1 but {why}")
            }
            Err(why) => {
                eprintln!("skipped: {why} (set LAZYAGENT_PYTHON)");
                return None;
            }
        };
        let tmp = tempfile::Builder::new().prefix("lazyagent-desktop-").tempdir().unwrap();
        let t = tmp.path();
        let bin = t.join("bin");
        std::fs::create_dir(&bin).unwrap();
        let fake = source_root().join("tests/engine/fake_agent.py");
        let shim = bin.join("claude");
        std::fs::write(&shim, format!("#!/bin/sh\nexec {} {} \"$@\"\n", python.display(), fake.display())).unwrap();
        std::fs::set_permissions(&shim, std::fs::Permissions::from_mode(0o755)).unwrap();
        let run = t.join("run");
        std::fs::create_dir(&run).unwrap();
        std::fs::set_permissions(&run, std::fs::Permissions::from_mode(0o700)).unwrap();
        let path = format!("{}:{}", bin.display(), std::env::var("PATH").unwrap_or_default());
        let mut env = Env::process()
            .with("HOME", Some(t.to_str().unwrap()))
            .with("XDG_RUNTIME_DIR", Some(run.to_str().unwrap()))
            .with("XDG_STATE_HOME", Some(t.join("state").to_str().unwrap()))
            .with("XDG_CONFIG_HOME", Some(t.join("config").to_str().unwrap()))
            .with("GIT_CONFIG_GLOBAL", Some("/dev/null"))
            .with("PATH", Some(&path))
            .with("TERM", Some("xterm-256color"))
            .with(discovery::PYTHON_ENV, Some(python.to_str().unwrap()));
        for name in [
            "LAZYAGENT_ENGINE",
            "LAZYAGENT_ENGINE_SOCKET",
            "LAZYAGENT_ENGINE_AUTOSTART",
            "LAZYAGENT_HOST",
            "LAZYAGENT_SOCKET",
            "LAZYAGENT_V1_SERVER",
            "TMUX",
        ] {
            env = env.with(name, None);
        }
        let repo = t.join("repo");
        std::fs::create_dir(&repo).unwrap();
        let mut world = World { repo: String::new(), tmp, env, python, engines: vec![] };
        world.git(&repo, &["init", "-q", "-b", "main"]);
        world.git(&repo, &["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"]);
        world.repo = repo.canonicalize().unwrap().to_string_lossy().into_owned();
        Some(world)
    }

    pub fn git(&self, cwd: &Path, args: &[&str]) -> String {
        let mut cmd = Command::new("git");
        cmd.args(args).current_dir(cwd);
        for (k, v) in self.env.vars() {
            cmd.env(k, v);
        }
        let out = cmd.output().unwrap();
        assert!(out.status.success(), "git {args:?}: {}", String::from_utf8_lossy(&out.stderr));
        String::from_utf8_lossy(&out.stdout).into_owned()
    }

    /// The socket path an engine for this repo listens on.
    pub fn socket_path(&self) -> PathBuf {
        discovery::runtime_dir(&self.repo, &self.env).join("engine.sock")
    }

    /// `python -m lazyagent.engine <repo>` in the foreground (its own
    /// process group); returns once its socket accepts connections.
    pub fn start_engine(&mut self) -> PathBuf {
        let mut cmd = Command::new(&self.python);
        cmd.args(["-m", "lazyagent.engine", &self.repo])
            .env_clear()
            .envs(self.env.vars())
            .current_dir(&self.repo)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(std::fs::File::create(self.tmp.path().join("engine.log")).unwrap())
            .process_group(0);
        self.engines.push(cmd.spawn().unwrap());
        let socket = self.socket_path();
        wait_for(TIMEOUT, "the engine socket", || discovery::alive(&socket));
        socket
    }

    pub fn engine_log(&self) -> String {
        std::fs::read_to_string(self.tmp.path().join("engine.log")).unwrap_or_default()
    }

    /// Connects and says hello as the desktop client.
    pub fn connect(&self) -> (Client, HelloResult, Receiver<ClientEvent>) {
        Client::connect(&self.socket_path(), "desktop", "test", TIMEOUT)
            .unwrap_or_else(|e| panic!("connect: {e}\nengine log:\n{}", self.engine_log()))
    }

    /// Kills every engine this world started, with their agents.
    pub fn kill_engines(&mut self) {
        for child in &mut self.engines {
            kill_group(child.id() as i32);
            let _ = child.wait();
        }
        self.engines.clear();
        // A detached engine (lazyagent engine start) is its own session.
        if let Some(pid) =
            discovery::engine_info(&self.repo, &self.env).and_then(|i| i.get("pid").and_then(|p| p.as_i64()))
        {
            kill_group(pid as i32);
        }
    }
}

fn kill_group(pgid: i32) {
    // SAFETY: plain kill(2) calls on a process group we created.
    unsafe {
        libc::kill(-pgid, libc::SIGTERM);
    }
    let deadline = Instant::now() + Duration::from_secs(3);
    // SAFETY: as above; signal 0 only probes.
    while Instant::now() < deadline && unsafe { libc::kill(-pgid, 0) } == 0 {
        std::thread::sleep(Duration::from_millis(20));
    }
    unsafe {
        libc::kill(-pgid, libc::SIGKILL);
    }
}

impl Drop for World {
    fn drop(&mut self) {
        self.kill_engines();
    }
}

pub fn wait_for(timeout: Duration, what: &str, mut ok: impl FnMut() -> bool) {
    let deadline = Instant::now() + timeout;
    while !ok() {
        assert!(Instant::now() < deadline, "{what} not met in {timeout:?}");
        std::thread::sleep(Duration::from_millis(5));
    }
}

/// Waits for the next event matching `pick`, dropping the others.
pub fn next_event<T>(rx: &Receiver<ClientEvent>, mut pick: impl FnMut(&ClientEvent) -> Option<T>) -> T {
    let deadline = Instant::now() + TIMEOUT;
    loop {
        let left = deadline.saturating_duration_since(Instant::now());
        let ev = rx.recv_timeout(left).unwrap_or_else(|e| panic!("no matching event: {e}"));
        if let Some(t) = pick(&ev) {
            return t;
        }
    }
}

/// A stream sink that keeps every byte and what happened to the stream.
#[derive(Default)]
pub struct Recorder {
    pub state: std::sync::Mutex<Recorded>,
    pub changed: std::sync::Condvar,
}

#[derive(Default, Debug)]
pub struct Recorded {
    pub bytes: Vec<u8>,
    /// Offset after the last byte received.
    pub end: Option<u64>,
    pub gaps: Vec<(u64, u64)>,
    pub resized: Vec<StreamGeometry>,
    pub reset: Option<StreamGeometry>,
    pub closed: Option<StreamClosed>,
}

impl Recorder {
    pub fn new() -> Arc<Recorder> {
        Arc::new(Recorder::default())
    }

    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.state.lock().unwrap().bytes).into_owned()
    }

    /// Waits until `ok` holds for the recorded state.
    pub fn wait(&self, what: &str, mut ok: impl FnMut(&Recorded) -> bool) {
        let guard = self.state.lock().unwrap();
        let (guard, res) = self.changed.wait_timeout_while(guard, TIMEOUT, |s| !ok(s)).unwrap();
        assert!(!res.timed_out(), "{what}: not seen; got {:?}", String::from_utf8_lossy(&guard.bytes));
    }

    pub fn wait_text(&self, needle: &str) {
        self.wait(needle, |s| String::from_utf8_lossy(&s.bytes).contains(needle));
    }
}

impl StreamSink for Recorder {
    fn output(&self, offset: u64, data: &[u8]) {
        let mut s = self.state.lock().unwrap();
        if let Some(end) = s.end
            && offset != end
        {
            s.gaps.push((end, offset));
        }
        s.end = Some(offset + data.len() as u64);
        s.bytes.extend_from_slice(data);
        self.changed.notify_all();
    }

    fn resized(&self, g: &StreamGeometry) {
        self.state.lock().unwrap().resized.push(g.clone());
        self.changed.notify_all();
    }

    fn reset(&self, g: &StreamGeometry) {
        self.state.lock().unwrap().reset = Some(g.clone());
        self.changed.notify_all();
    }

    fn closed(&self, c: &StreamClosed) {
        self.state.lock().unwrap().closed = Some(c.clone());
        self.changed.notify_all();
    }
}
