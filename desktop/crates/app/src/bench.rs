//! Frame hooks: `--screenshot` and `--bench NAME`. Benches run against the
//! repo's real engine (use a scratch repo whose `claude` is the fake agent,
//! tests/engine/fake_agent.py), print one JSON line, and close the window.
//!
//! Latency is measured inside the app, the way the web page's bench did it
//! (ui-plan §7d-1 B1): from the moment the keystroke's bytes leave
//! (`Pane::write`) to the end of the frame that draws the echo ("built"),
//! and to the start of the frame after it ("next"), which egui only begins
//! once the echo's frame went to the GPU and the surface took it.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use eframe::egui;
use serde_json::json;

use crate::app::{FrameHook, State};
use crate::panes::Pane;
use crate::store::ORCHESTRATOR_KEY;

pub fn create(name: &str, t0: Instant) -> Result<Box<dyn FrameHook>, String> {
    Ok(match name {
        "echo" => Box::new(Echo::default()),
        "flood" => Box::new(Flood::default()),
        "idle" => Box::new(Idle::default()),
        "attach" => Box::new(Attach::default()),
        "startup" => Box::new(Startup { t0, seeded: None }),
        other => return Err(format!("unknown bench {other} (echo, flood, idle, attach, startup)")),
    })
}

pub struct Screenshot {
    path: PathBuf,
    at: Instant,
    asked: bool,
    done: Arc<std::sync::atomic::AtomicBool>,
}

impl Screenshot {
    pub fn new(path: PathBuf, after_ms: u64) -> Screenshot {
        Screenshot {
            path,
            at: Instant::now() + Duration::from_millis(after_ms),
            asked: false,
            done: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        }
    }
}

pub fn save_png(image: &egui::ColorImage, path: &std::path::Path) -> Result<(), String> {
    let [w, h] = image.size;
    let bytes: Vec<u8> = image.pixels.iter().flat_map(|c| c.to_srgba_unmultiplied()).collect();
    image::save_buffer(path, &bytes, w as u32, h as u32, image::ExtendedColorType::Rgba8).map_err(|e| e.to_string())
}

impl FrameHook for Screenshot {
    fn frame(&mut self, _state: &mut State, ctx: &egui::Context) {
        if self.done.load(std::sync::atomic::Ordering::SeqCst) {
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
            return;
        }
        let now = Instant::now();
        if now < self.at {
            ctx.request_repaint_after(self.at - now);
            return;
        }
        if !self.asked {
            self.asked = true;
            let path = self.path.clone();
            let done = self.done.clone();
            let ctx2 = ctx.clone();
            ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(egui::ScreenshotCallback::new(move |image| {
                match save_png(&image, &path) {
                    Ok(()) => eprintln!("screenshot: {}", path.display()),
                    Err(e) => eprintln!("screenshot failed: {e}"),
                }
                done.store(true, std::sync::atomic::Ordering::SeqCst);
                ctx2.request_repaint();
            })));
        }
        ctx.request_repaint_after(Duration::from_millis(50));
    }
}

fn percentiles(mut v: Vec<f64>) -> serde_json::Value {
    if v.is_empty() {
        return json!(null);
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let p = |q: f64| (v[((v.len() - 1) as f64 * q).round() as usize] * 100.0).round() / 100.0;
    json!({"n": v.len(), "p50": p(0.5), "p95": p(0.95), "p99": p(0.99), "max": p(1.0)})
}

/// utime + stime of a process, in seconds.
pub fn cpu_seconds(pid: u32) -> f64 {
    let Ok(s) = std::fs::read_to_string(format!("/proc/{pid}/stat")) else { return 0.0 };
    let f: Vec<&str> = s.rsplit(')').next().unwrap_or("").split_whitespace().collect();
    let tick = |i: usize| f.get(i).and_then(|v| v.parse::<f64>().ok()).unwrap_or(0.0);
    (tick(11) + tick(12)) / 100.0
}

/// (RSS, private dirty) in MB.
pub fn memory_mb(pid: u32) -> (f64, f64) {
    let text = std::fs::read_to_string(format!("/proc/{pid}/smaps_rollup")).unwrap_or_default();
    let get = |key: &str| {
        text.lines()
            .find(|l| l.starts_with(key))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(0.0)
            / 1024.0
    };
    // Dirty private pages: what the process really costs (clean ones are
    // file pages, e.g. GPU driver libraries, that the kernel can drop).
    (get("Rss:"), get("Private_Dirty:"))
}

fn engine_pid(state: &State) -> Option<u32> {
    lazyagent_proto::discovery::engine_info(&state.conn.root, &state.conn.env)
        .and_then(|i| i.get("pid").and_then(|p| p.as_u64()))
        .map(|p| p as u32)
}

fn finish(ctx: &egui::Context, result: serde_json::Value) {
    static DONE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
    if !DONE.swap(true, std::sync::atomic::Ordering::SeqCst) {
        println!("{result}");
    }
    ctx.send_viewport_cmd(egui::ViewportCommand::Close);
}

/// Spawns one agent in the main worktree and waits until its pane is
/// drawn with the fake agent's prompt; then `ready` returns the pane.
#[derive(Default)]
struct Setup {
    asked: bool,
    session: Option<String>,
    deadline: Option<Instant>,
}

impl Setup {
    fn ready(&mut self, state: &mut State, ctx: &egui::Context) -> Option<Arc<Pane>> {
        let deadline = *self.deadline.get_or_insert(Instant::now() + Duration::from_secs(30));
        if Instant::now() > deadline {
            finish(ctx, json!({"error": "setup timed out", "link": format!("{:?}", state.link)}));
            return None;
        }
        ctx.request_repaint_after(Duration::from_millis(20));
        if !state.seeded {
            return None;
        }
        let main = state.store.worktrees.iter().find(|w| w.is_main)?.path.clone();
        if !self.asked {
            self.asked = true;
            state.selected = Some(main.clone());
            state.spawn_agent(Some(main.clone()));
            return None;
        }
        if self.session.is_none() {
            let tab = state.tabs.get(&main)?;
            self.session = state.store.agent(&main, tab)?.session_id.clone();
        }
        let pane = state.panes.get(self.session.as_ref()?)?.clone();
        let text = pane.inner.lock().unwrap().term.frame().text();
        (pane.attached() && text.contains("your turn>")).then_some(pane)
    }
}

#[derive(Default)]
struct Echo {
    setup: Setup,
    pane: Option<Arc<Pane>>,
    sent: Option<(Instant, u8)>,
    built_at: Option<Instant>,
    built: Vec<f64>,
    next: Vec<f64>,
    round_trip: Vec<f64>,
    wait_frame: Vec<f64>,
    frame_cpu: Vec<f64>,
    build: Vec<f64>,
    n: usize,
    wait_until: Option<Instant>,
    /// B2: hidden agents printing ~1,000 lines/s while we measure.
    busy_started: Option<Instant>,
    busy_spawned: Arc<std::sync::atomic::AtomicUsize>,
}

const ECHO_WARMUP: usize = 30;
const ECHO_SAMPLES: usize = 300;

impl FrameHook for Echo {
    fn frame(&mut self, state: &mut State, ctx: &egui::Context) {
        let now = Instant::now();
        let Some(pane) = self.pane.clone().or_else(|| self.setup.ready(state, ctx)) else { return };
        self.pane = Some(pane.clone());
        let busy: usize = std::env::var("LAZYAGENT_BENCH_BUSY").ok().and_then(|v| v.parse().ok()).unwrap_or(0);
        if busy > 0 {
            let main = state.selected.clone().unwrap();
            if self.busy_started.is_none() {
                self.busy_started = Some(now);
                for i in 0..busy {
                    let counter = self.busy_spawned.clone();
                    let conn = state.conn.clone();
                    let params = json!({"worktree_path": main, "skip_permissions": false, "label": format!("busy {i}"),
                                        "env": state.conn.env.vars().into_iter().collect::<std::collections::BTreeMap<_, _>>()});
                    let main2 = main.clone();
                    state.conn.request("spawn_agent", params, move |_s, reply| {
                        let Ok(v) = reply else { return };
                        let id = v["agent_id"].as_str().unwrap_or_default().to_string();
                        conn.request(
                            "send_agent_input",
                            json!({"worktree_path": main2, "agent_id": id, "text": "count 60000"}),
                            move |_s, _r| {
                                counter.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                            },
                        );
                    });
                }
            }
            if self.busy_spawned.load(std::sync::atomic::Ordering::SeqCst) < busy
                || now - self.busy_started.unwrap() < Duration::from_secs(3)
            {
                ctx.request_repaint_after(Duration::from_millis(50));
                return;
            }
        }
        // The frame after the echo's: the echo's frame has been presented.
        if let Some(built) = self.built_at.take() {
            let (sent, _) = self.sent.take().unwrap();
            if self.n >= ECHO_WARMUP {
                self.built.push((built - sent).as_secs_f64() * 1e3);
                self.next.push((now - sent).as_secs_f64() * 1e3);
            }
            self.n += 1;
            self.wait_until = Some(now + Duration::from_millis(5));
        }
        if let Some((sent, key)) = self.sent {
            let i = pane.inner.lock().unwrap();
            let echoed =
                i.last_output.is_some_and(|t| t > sent) && i.term.frame().text().trim_end().ends_with(key as char);
            let output_at = i.last_output;
            drop(i);
            let drawn = state.views.get(&pane.session_id).and_then(|v| v.meshes.key).map(|k| k.generation);
            let generation = pane.inner.lock().unwrap().term.generation();
            if echoed && drawn == Some(generation) {
                self.built_at = Some(now);
                if let Some(o) = output_at
                    && self.n >= ECHO_WARMUP
                {
                    self.round_trip.push((o - sent).as_secs_f64() * 1e3);
                    self.wait_frame.push(state.frame_started.saturating_duration_since(o).as_secs_f64() * 1e3);
                    self.build.push((now - state.frame_started).as_secs_f64() * 1e3);
                    if let Some(c) = state.last_frame_cpu {
                        self.frame_cpu.push(c as f64 * 1e3);
                    }
                }
                ctx.request_repaint();
            }
            if now - sent > Duration::from_secs(5) {
                finish(ctx, json!({"bench": "echo", "error": "no echo in 5 s"}));
            }
            return;
        }
        if self.n >= ECHO_WARMUP + ECHO_SAMPLES {
            finish(
                ctx,
                json!({
                    "bench": "echo",
                    "busy_hidden_agents": std::env::var("LAZYAGENT_BENCH_BUSY").unwrap_or_default(),
                    "key_to_bytes_ms": percentiles(std::mem::take(&mut self.round_trip)),
                    "bytes_to_frame_start_ms": percentiles(std::mem::take(&mut self.wait_frame)),
                    "frame_build_ms": percentiles(std::mem::take(&mut self.build)),
                    "previous_frame_cpu_ms": percentiles(std::mem::take(&mut self.frame_cpu)),
                    "key_to_frame_built_ms": percentiles(std::mem::take(&mut self.built)),
                    "key_to_next_frame_ms": percentiles(std::mem::take(&mut self.next)),
                    "backend": std::env::var("WGPU_BACKEND").unwrap_or_else(|_| "default".into()),
                    "present": std::env::var(crate::PRESENT_ENV).unwrap_or_else(|_| "default".into()),
                }),
            );
            return;
        }
        if let Some(w) = self.wait_until {
            if now < w {
                ctx.request_repaint_after(w - now);
                return;
            }
            self.wait_until = None;
        }
        let key = b'a' + (self.n % 26) as u8;
        if std::env::var("LAZYAGENT_BENCH_OFF_FRAME").is_ok() {
            // From another thread once this frame is done: the echo's frame
            // then starts from an idle event loop (the wake-up path alone).
            let sent = Instant::now() + Duration::from_millis(4);
            self.sent = Some((sent, key));
            let p = pane.clone();
            std::thread::spawn(move || {
                std::thread::sleep(sent.saturating_duration_since(Instant::now()));
                p.write(&[key]);
            });
            return;
        }
        self.sent = Some((Instant::now(), key));
        pane.write(&[key]);
    }
}

#[derive(Default)]
struct Flood {
    setup: Setup,
    pane: Option<Arc<Pane>>,
    start: Option<Instant>,
    last_frame: Option<Instant>,
    intervals: Vec<f64>,
    ui_cpu0: f64,
    engine_cpu0: f64,
    bytes0: u64,
}

const FLOOD_BYTES: u64 = 5_000_000;

impl FrameHook for Flood {
    fn frame(&mut self, state: &mut State, ctx: &egui::Context) {
        let Some(pane) = self.pane.clone().or_else(|| self.setup.ready(state, ctx)) else { return };
        let now = Instant::now();
        let me = std::process::id();
        if self.pane.is_none() {
            self.pane = Some(pane.clone());
            self.start = Some(now);
            self.ui_cpu0 = cpu_seconds(me);
            self.engine_cpu0 = engine_pid(state).map(cpu_seconds).unwrap_or(0.0);
            self.bytes0 = pane.inner.lock().unwrap().bytes_in;
            pane.write(format!("spew {FLOOD_BYTES}\r").as_bytes());
            ctx.request_repaint();
            return;
        }
        if let Some(l) = self.last_frame {
            self.intervals.push((now - l).as_secs_f64() * 1e3);
        }
        self.last_frame = Some(now);
        // Done once the final screen is drawn: the stream may have been
        // dropped and re-attached on the way (the engine skips what a slow
        // client cannot take), so bytes received can be fewer than sent.
        let (bytes, resets, done) = {
            let i = pane.inner.lock().unwrap();
            let text = i.term.frame().text();
            let text = text.trim_end();
            (i.bytes_in - self.bytes0, i.resets, text.ends_with("your turn>") && text.contains("done"))
        };
        let elapsed = now - self.start.unwrap();
        if done || elapsed > Duration::from_secs(60) {
            let secs = elapsed.as_secs_f64();
            let long = self.intervals.iter().filter(|v| **v > 50.0).count();
            finish(
                ctx,
                json!({
                    "bench": "flood",
                    "bytes_parsed": bytes,
                    "stream_resets": resets,
                    "seconds": (secs * 1000.0).round() / 1000.0,
                    "mb_per_s": ((FLOOD_BYTES as f64 / 1e6 / secs) * 10.0).round() / 10.0,
                    "frame_interval_ms": percentiles(std::mem::take(&mut self.intervals)),
                    "frames_over_50ms": long,
                    "app_cpu_s": ((cpu_seconds(me) - self.ui_cpu0) * 100.0).round() / 100.0,
                    "engine_cpu_s": ((engine_pid(state).map(cpu_seconds).unwrap_or(0.0) - self.engine_cpu0) * 100.0).round() / 100.0,
                    "completed": done,
                }),
            );
        }
    }
}

#[derive(Default)]
struct Idle {
    setup: Setup,
    since: Option<(Instant, f64, f64, u64)>,
}

const IDLE_FOR: Duration = Duration::from_secs(10);

impl FrameHook for Idle {
    fn frame(&mut self, state: &mut State, ctx: &egui::Context) {
        let me = std::process::id();
        if self.since.is_some() {
            return self.measure(state, ctx);
        }
        if self.setup.ready(state, ctx).is_none() {
            return;
        }
        // Settled: measure with no repaint asked by the bench until the end.
        let engine = engine_pid(state).map(cpu_seconds).unwrap_or(0.0);
        self.since = Some((Instant::now(), cpu_seconds(me), engine, state.frames));
        ctx.request_repaint_after(IDLE_FOR);
    }
}

impl Idle {
    fn measure(&mut self, state: &mut State, ctx: &egui::Context) {
        let me = std::process::id();
        let (t, cpu0, engine0, frames0) = self.since.unwrap();
        let elapsed = t.elapsed();
        if elapsed < IDLE_FOR {
            ctx.request_repaint_after(IDLE_FOR - elapsed);
            return;
        }
        let secs = elapsed.as_secs_f64();
        let (rss, private) = memory_mb(me);
        let pct = |v: f64| (v / secs * 1000.0).round() / 10.0;
        finish(
            ctx,
            json!({
                "bench": "idle",
                "seconds": secs.round(),
                "frames": state.frames - frames0,
                "app_cpu_pct": pct(cpu_seconds(me) - cpu0),
                "engine_cpu_pct": pct(engine_pid(state).map(cpu_seconds).unwrap_or(0.0) - engine0),
                "rss_mb": rss.round(),
                "private_mb": private.round(),
                "backend": std::env::var("WGPU_BACKEND").unwrap_or_else(|_| "default".into()),
                "present": std::env::var(crate::PRESENT_ENV).unwrap_or_else(|_| "default".into()),
            }),
        );
    }
}

#[derive(Default)]
struct Attach {
    setup: Setup,
    ready: bool,
    current: Option<(Instant, Arc<Pane>)>,
    times: Vec<f64>,
}

impl FrameHook for Attach {
    fn frame(&mut self, state: &mut State, ctx: &egui::Context) {
        let Some(pane) = (if self.ready { None } else { self.setup.ready(state, ctx) })
            .or_else(|| self.ready.then(|| self.current.as_ref().map(|c| c.1.clone())).flatten())
        else {
            if !self.ready {
                return;
            }
            return;
        };
        if !self.ready {
            self.ready = true;
            // History to snapshot.
            pane.write(b"count 2000\r");
            std::thread::sleep(Duration::from_millis(3000));
        }
        ctx.request_repaint_after(Duration::from_millis(5));
        if let Some((t, p)) = &self.current {
            if !p.attached() {
                return;
            }
            self.times.push(t.elapsed().as_secs_f64() * 1e3);
            p.detach();
        }
        if self.times.len() >= 25 {
            finish(ctx, json!({"bench": "attach", "attach_snapshot_ms": percentiles(std::mem::take(&mut self.times))}));
            return;
        }
        let Some(client) = state.conn.client() else { return };
        let fresh =
            Pane::new(pane.session_id.clone(), pane.kind, state.theme.palette(), ctx.clone(), state.pacer.clone());
        let t = Instant::now();
        fresh.attach(&client);
        self.current = Some((t, fresh));
    }
}

struct Startup {
    t0: Instant,
    seeded: Option<f64>,
}

impl FrameHook for Startup {
    fn frame(&mut self, state: &mut State, ctx: &egui::Context) {
        ctx.request_repaint_after(Duration::from_millis(5));
        if self.seeded.is_none() && state.seeded {
            self.seeded = Some(self.t0.elapsed().as_secs_f64() * 1e3);
        }
        let Some(seeded) = self.seeded else {
            if self.t0.elapsed() > Duration::from_secs(30) {
                finish(ctx, json!({"bench": "startup", "error": "never connected"}));
            }
            return;
        };
        // With an agent: until its terminal shows content; without: the sidebar.
        let selected = state.selected.clone().unwrap_or_default();
        let has_agent = selected != ORCHESTRATOR_KEY && !state.store.agents(&selected).is_empty();
        let drawn = state.views.values().any(|v| v.meshes.key.is_some() && !v.frame.text().trim().is_empty());
        if !has_agent || drawn || self.t0.elapsed() > Duration::from_secs(30) {
            finish(
                ctx,
                json!({
                    "bench": "startup",
                    "process_to_sidebar_ms": seeded.round(),
                    "process_to_terminal_ms": has_agent.then(|| (self.t0.elapsed().as_secs_f64() * 1e3).round()),
                }),
            );
        }
    }
}
