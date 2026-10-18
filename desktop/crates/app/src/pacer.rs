//! Repaints asked from other threads (terminal output), paced: at once
//! when the last output-driven repaint is at least one refresh old, else
//! at the next slot. An echo is drawn immediately (even right after the
//! keypress's own frame); a flood is drawn at the refresh rate however
//! many chunks arrive, with or without vsync.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use eframe::egui;

pub struct Pacer {
    last: Mutex<Option<Instant>>,
    interval: Duration,
}

impl Pacer {
    pub fn new(interval: Duration) -> Pacer {
        Pacer { last: Mutex::new(None), interval }
    }

    pub fn request(&self, ctx: &egui::Context) {
        let now = Instant::now();
        let mut last = self.last.lock().unwrap();
        match *last {
            Some(t) if now.duration_since(t) < self.interval => {
                ctx.request_repaint_after(self.interval - now.duration_since(t));
            }
            _ => {
                *last = Some(now);
                ctx.request_repaint();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn immediate_then_deferred_then_immediate() {
        let ctx = egui::Context::default();
        let p = Pacer::new(Duration::from_millis(30));
        p.request(&ctx);
        let first = p.last.lock().unwrap().unwrap();
        p.request(&ctx);
        assert_eq!(p.last.lock().unwrap().unwrap(), first, "a second request within the interval is deferred");
        std::thread::sleep(Duration::from_millis(35));
        p.request(&ctx);
        assert!(p.last.lock().unwrap().unwrap() > first);
    }
}
