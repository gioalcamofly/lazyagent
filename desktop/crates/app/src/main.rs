//! `lazyagent-desktop [repo]`: the native desktop app for lazyagent.

mod app;
mod attention;
mod attention_view;
mod bench;
mod boxdraw;
mod conn;
mod dialogs;
mod fonts;
mod gpu;
mod notify;
mod pacer;
mod panes;
mod persist;
mod render;
mod store;
mod term_view;
#[cfg(test)]
mod tests;
mod theme;

use std::path::PathBuf;
use std::sync::mpsc;

use eframe::egui;
use lazyagent_proto::discovery::{self, Env};

use crate::app::{App, FrameHook, State};
use crate::theme::ThemeKind;

const USAGE: &str = "\
usage: lazyagent-desktop [repo] [options]

The lazyagent desktop app: the repo's worktrees, agents and their terminals.
It connects to the repo's engine, starting one when none runs.

options:
  --theme dark|light       colour theme (default dark)
  --screenshot PATH        save a PNG of the window after --after-ms, then exit
  --after-ms N             delay before the screenshot (default 1500)
  --bench NAME             run a benchmark and print JSON (echo, flood, idle, attach, startup)
  -h, --help               this text

environment:
  LAZYAGENT_PYTHON              Python that runs `-m lazyagent engine start`
  LAZYAGENT_ENGINE_SOCKET       use this engine socket (no discovery, no auto-start)
  LAZYAGENT_ENGINE_AUTOSTART=0  never start an engine
  LAZYAGENT_DESKTOP_FONT        terminal font family (default DejaVu Sans Mono)
  LAZYAGENT_DESKTOP_FONT_SIZE   terminal font size (default 13)
  WGPU_BACKEND                  vulkan or gl
  LAZYAGENT_DESKTOP_PRESENT     fifo, immediate, mailbox, novsync (default), auto
  LAZYAGENT_DESKTOP_ALL_GPUS=1  keep an offload NVIDIA GPU's Vulkan driver loaded
";

/// `fifo` (vsync), `immediate` (no vsync, may tear), `mailbox`, `auto`.
pub const PRESENT_ENV: &str = "LAZYAGENT_DESKTOP_PRESENT";

fn present_mode() -> Option<eframe::wgpu::PresentMode> {
    use eframe::wgpu::PresentMode;
    Some(match std::env::var(PRESENT_ENV).ok()?.as_str() {
        "fifo" => PresentMode::Fifo,
        "immediate" => PresentMode::Immediate,
        "mailbox" => PresentMode::Mailbox,
        "novsync" => PresentMode::AutoNoVsync,
        "auto" => PresentMode::AutoVsync,
        _ => return None,
    })
}

pub struct Args {
    pub repo: Option<PathBuf>,
    pub theme: Option<ThemeKind>,
    pub screenshot: Option<PathBuf>,
    pub after_ms: u64,
    pub bench: Option<String>,
}

fn parse_args() -> Result<Args, String> {
    let mut args = Args { repo: None, theme: None, screenshot: None, after_ms: 1500, bench: None };
    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        let mut value = |name: &str| it.next().ok_or(format!("{name} needs a value"));
        match a.as_str() {
            "-h" | "--help" => {
                print!("{USAGE}");
                std::process::exit(0);
            }
            "--theme" => {
                args.theme = match value("--theme")?.as_str() {
                    "light" => Some(ThemeKind::Light),
                    "dark" => Some(ThemeKind::Dark),
                    other => return Err(format!("unknown theme {other}")),
                }
            }
            "--screenshot" => args.screenshot = Some(value("--screenshot")?.into()),
            "--after-ms" => args.after_ms = value("--after-ms")?.parse().map_err(|_| "--after-ms needs a number")?,
            "--bench" => args.bench = Some(value("--bench")?),
            s if s.starts_with('-') => return Err(format!("unknown option {s}\n\n{USAGE}")),
            s => args.repo = Some(s.into()),
        }
    }
    Ok(args)
}

fn main() {
    let t0 = std::time::Instant::now();
    // First, while single-threaded: it sets an environment variable.
    let hidden_gpu = gpu::hide_offload_nvidia();
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("lazyagent-desktop: {e}");
            std::process::exit(2);
        }
    };
    let root = match discovery::repo_root(args.repo.as_deref(), None) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("lazyagent-desktop: {e}");
            std::process::exit(2);
        }
    };
    // Agents and shells get the environment we were started with.
    let mut env = Env::process();
    if hidden_gpu.is_some() {
        env = env.with(gpu::ICD_ENV, None);
    }
    // Fonts load while the window and the GPU come up.
    let fonts = std::thread::spawn(fonts::FontSet::discover);

    let mut options = eframe::NativeOptions::default();
    let title = format!("lazyagent — {}", store::basename(&root));
    options.viewport = egui::ViewportBuilder::default()
        .with_title(title)
        .with_app_id("lazyagent-desktop")
        .with_inner_size([1400.0, 880.0])
        .with_min_inner_size([640.0, 400.0]);
    // Lowest latency the surface offers without tearing: one frame queued.
    options.wgpu_options.surface.desired_maximum_frame_latency = Some(1);
    // No vsync wait between a key and its echo (Immediate, else Mailbox,
    // else Fifo): measured 4 ms instead of a full refresh. The pacer keeps
    // output-driven frames at the refresh rate.
    options.wgpu_options.surface.present_mode = present_mode().unwrap_or(eframe::wgpu::PresentMode::AutoNoVsync);

    let bench_name = args.bench.clone();
    let event_loop = match winit::event_loop::EventLoop::<eframe::UserEvent>::with_user_event().build() {
        Ok(l) => l,
        Err(e) => {
            eprintln!("lazyagent-desktop: no display: {e}");
            std::process::exit(1);
        }
    };
    // Raw device events (XInput2 motion anywhere on screen) would wake the
    // event loop on every mouse move, window or not: ~1 % of a core idle.
    event_loop.listen_device_events(winit::event_loop::DeviceEvents::Never);
    let mut app = eframe::create_native(
        "lazyagent-desktop",
        options,
        Box::new(move |cc| {
            let ctx = cc.egui_ctx.clone();
            let (tx, rx) = mpsc::channel();
            let conn = conn::Conn::new(root, env, tx, ctx.clone());
            conn.spawn_supervisor();
            let fonts = fonts.join().unwrap_or_else(|_| fonts::FontSet::bundled_only());
            eprintln!("lazyagent-desktop: terminal font {} {} px", fonts.family, fonts::font_size());
            let state = State::new(conn, fonts, args.theme);
            state.theme.apply(&ctx);
            let hook: Option<Box<dyn FrameHook>> = match (&bench_name, &args.screenshot) {
                (Some(name), _) => Some(bench::create(name, t0).unwrap_or_else(|e| {
                    eprintln!("lazyagent-desktop: {e}");
                    std::process::exit(2)
                })),
                (None, Some(path)) => Some(Box::new(bench::Screenshot::new(path.clone(), args.after_ms))),
                _ => None,
            };
            Ok(Box::new(App::new(state, rx, hook)))
        }),
        &event_loop,
    );
    if let Err(e) = event_loop.run_app(&mut app) {
        eprintln!("lazyagent-desktop: {e}");
        std::process::exit(1);
    }
}
