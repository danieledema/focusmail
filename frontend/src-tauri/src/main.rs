#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Deserialize;
use std::{fs, io::{BufRead, BufReader}, process::{Child, Command, Stdio}, sync::{mpsc, Mutex}, time::Duration};
use tauri_plugin_opener::OpenerExt;
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

#[derive(Deserialize)]
struct Ready { port: u16, token: String }
struct Backend(Mutex<Option<Child>>);

impl Backend {
    fn stop(&self) {
        if let Ok(mut guard) = self.0.lock() {
            if let Some(mut child) = guard.take() {
                // Closing the readiness/parent pipe asks the backend to stop.
                child.stdin.take();
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}
impl Drop for Backend { fn drop(&mut self) { self.stop(); } }

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .on_window_event(|window, event| {
            if matches!(event, tauri::WindowEvent::CloseRequested { .. }) {
                window.app_handle().exit(0);
            }
        })
        .setup(|app| {
            let mut data = app.path().app_data_dir()?;
            let demo = std::env::var("FOCUSMAIL_DESKTOP_DEMO").as_deref() == Ok("1");
            if demo { data.push("demo"); }
            fs::create_dir_all(&data)?;
            let log_path = data.join("desktop.log");
            if fs::metadata(&log_path).map(|m| m.len() > 1_000_000).unwrap_or(false) {
                fs::write(&log_path, "")?;
            }
            let log = fs::OpenOptions::new().create(true).append(true).open(&log_path)?;
            let binary = std::env::current_exe()?.parent().ok_or("Missing app directory")?
                .join(if cfg!(windows) { "focusmail-backend.exe" } else { "focusmail-backend" });
            // The sidecar is copied beside the executable by Tauri's bundler/CLI.
            let mut command = Command::new(binary);
            command.arg("--data-dir").arg(&data).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(log);
            if demo { command.arg("--demo"); }
            #[cfg(windows)] {
                use std::os::windows::process::CommandExt;
                command.creation_flags(0x08000000); // CREATE_NO_WINDOW
            }
            let mut child = command.spawn()?;
            let stdout = child.stdout.take().ok_or("Missing backend readiness pipe")?;
            let backend = Backend(Mutex::new(Some(child)));
            let (sender, receiver) = mpsc::channel();
            std::thread::spawn(move || {
                let mut line = String::new();
                let result = BufReader::new(stdout).read_line(&mut line).map(|_| line);
                let _ = sender.send(result);
            });
            let line = receiver.recv_timeout(Duration::from_secs(60))
                .map_err(|_| "FocusMail backend did not start. See desktop.log in the application data folder.")??;
            let ready: Ready = serde_json::from_str(&line)?;
            if ready.port == 0 || ready.token.len() < 32 || !ready.token.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
                return Err("Invalid backend startup response".into());
            }
            let port = ready.port;
            let url = format!("http://127.0.0.1:{port}/desktop/start/{}", ready.token).parse()?;
            app.manage(backend);
            let navigation_app = app.handle().clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
                .title(if demo { "FocusMail — Demo" } else { "FocusMail" })
                .inner_size(1320.0, 900.0).min_inner_size(720.0, 600.0)
                .on_navigation(move |url| {
                    let owned_backend = url.scheme() == "http" && url.host_str() == Some("127.0.0.1") && url.port() == Some(port);
                    if owned_backend { return true; }
                    // Email links and OAuth run in the system browser; other schemes stay blocked.
                    if matches!(url.scheme(), "https" | "http" | "mailto") {
                        let _ = navigation_app.opener().open_url(url.as_str(), None::<&str>);
                    }
                    false
                })
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Unable to start FocusMail desktop");
    app.run(|app, event| {
        if matches!(event, RunEvent::Exit) { app.state::<Backend>().stop(); }
    });
}
