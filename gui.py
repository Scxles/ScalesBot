import os
import sys
import time
import queue
import threading
import subprocess

import tkinter as tk
from tkinter import ttk, messagebox


APP_TITLE = "Scales Bot Controller"
BOT_SCRIPT = "bot.py"
REQ_FILE = "requirements.txt"

# Place one of these in the same folder as gui.py to override the window/taskbar icon.
# Windows strongly prefers .ico for best results.
ICON_ICO = "icon.ico"
ICON_PNG = "icon.png"


def project_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def is_windows() -> bool:
    return os.name == "nt"


def open_path(path: str) -> None:
    """Open a folder/file in the OS default handler."""
    try:
        if is_windows():
            os.startfile(path)  # type: ignore[attr-defined]
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
            return
        subprocess.Popen(["xdg-open", path])
    except Exception:
        # Don't crash the GUI on open failures.
        pass


def try_set_windows_app_user_model_id(app_id: str) -> None:
    """Helps Windows group the taskbar icon under a stable identity (instead of python.exe)."""
    if not is_windows():
        return
    try:
        import ctypes  # noqa: PLC0415

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        # Non-fatal.
        pass


class BotControllerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.minsize(900, 520)

        self.root_dir = project_root()
        # Keep a reference to avoid Tk photo images being garbage-collected.
        self._icon_image_ref: tk.PhotoImage | None = None
        self._apply_window_icon()

        self.proc: subprocess.Popen[str] | None = None
        self._proc_lock = threading.Lock()
        self.reader_thread: threading.Thread | None = None
        self.q: queue.Queue[tuple[str, str]] = queue.Queue()
        self.connected = False
        self.start_ts: float | None = None
        self._starting = False

        # UI state
        self.status_var = tk.StringVar(value="Stopped")
        self.detail_var = tk.StringVar(value="")
        self.autostart_var = tk.BooleanVar(value=False)
        self.pip_on_start_var = tk.BooleanVar(value=True)

        self._build_ui()

        # pump output queue
        self.root.after(100, self._pump_queue)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if self.autostart_var.get():
            self.start_bot()

    # ---------------- UI ----------------

    def _apply_window_icon(self) -> None:
        """Set top-left/titlebar icon and taskbar icon.

        Usage:
          - Put 'icon.ico' (recommended on Windows) OR 'icon.png' in the same folder as gui.py.
        """
        ico_path = os.path.join(self.root_dir, ICON_ICO)
        png_path = os.path.join(self.root_dir, ICON_PNG)

        # Best for Windows: .ico via iconbitmap
        if is_windows() and os.path.exists(ico_path):
            try:
                self.root.iconbitmap(ico_path)
                return
            except Exception:
                # Fall back to png below
                pass

        # Cross-platform fallback: .png via iconphoto
        if os.path.exists(png_path):
            try:
                img = tk.PhotoImage(file=png_path)
                self.root.iconphoto(True, img)
                self._icon_image_ref = img
            except Exception:
                pass

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        top = ttk.Frame(self.root, padding=10)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(3, weight=1)

        ttk.Label(top, text="Bot Status:").grid(row=0, column=0, sticky="w")
        self.status_badge = ttk.Label(top, textvariable=self.status_var)
        self.status_badge.grid(row=0, column=1, sticky="w", padx=(6, 18))

        ttk.Label(top, textvariable=self.detail_var).grid(row=0, column=2, sticky="w")

        btns = ttk.Frame(top)
        btns.grid(row=0, column=4, sticky="e")

        self.start_btn = ttk.Button(btns, text="Start", command=self.start_bot)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop_bot)
        self.restart_btn = ttk.Button(btns, text="Restart", command=self.restart_bot)
        self.clear_btn = ttk.Button(btns, text="Clear Logs", command=self.clear_logs)

        self.start_btn.grid(row=0, column=0, padx=3)
        self.stop_btn.grid(row=0, column=1, padx=3)
        self.restart_btn.grid(row=0, column=2, padx=3)
        self.clear_btn.grid(row=0, column=3, padx=3)

        mid = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        mid.grid(row=1, column=0, sticky="nsew")
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)

        # Log view
        log_frame = ttk.Labelframe(mid, text="Logs", padding=8)
        log_frame.grid(row=0, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_frame,
            wrap="none",
            height=20,
            undo=False,
        )
        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        xscroll = ttk.Scrollbar(log_frame, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        self.log_text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")

        # Bottom controls
        bottom = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(5, weight=1)

        ttk.Checkbutton(bottom, text="Auto-start when controller opens", variable=self.autostart_var).grid(
            row=0, column=0, sticky="w", padx=(0, 18)
        )
        ttk.Checkbutton(bottom, text="Run pip install on Start", variable=self.pip_on_start_var).grid(
            row=0, column=1, sticky="w", padx=(0, 18)
        )

        ttk.Button(bottom, text="Open Folder", command=lambda: open_path(self.root_dir)).grid(
            row=0, column=2, padx=3
        )
        ttk.Button(bottom, text="Open config.json", command=self._open_config).grid(row=0, column=3, padx=3)
        ttk.Button(bottom, text="Open .env", command=self._open_env).grid(row=0, column=4, padx=3)

        self._refresh_buttons()
        self._set_status("Stopped")

    def _open_config(self) -> None:
        open_path(os.path.join(self.root_dir, "config.json"))

    def _open_env(self) -> None:
        open_path(os.path.join(self.root_dir, ".env"))

    # ---------------- Bot control ----------------

    def start_bot(self) -> None:
        with self._proc_lock:
            if self._starting:
                return
            if self.proc and self.proc.poll() is None:
                return

        bot_path = os.path.join(self.root_dir, BOT_SCRIPT)
        if not os.path.exists(bot_path):
            self._append_line(f"ERROR: Missing {BOT_SCRIPT} in {self.root_dir}")
            return

        self.connected = False
        self.start_ts = time.time()
        self._starting = True
        self._set_status("Starting")
        self._refresh_buttons()

        threading.Thread(target=self._start_sequence, daemon=True).start()

    def stop_bot(self) -> None:
        with self._proc_lock:
            if not self.proc or self.proc.poll() is not None:
                self.proc = None
                self.connected = False
                self._set_status("Stopped")
                self._refresh_buttons()
                return

        self._append_line("[controller] Stopping bot...")

        try:
            with self._proc_lock:
                if self.proc:
                    self.proc.terminate()
        except Exception:
            pass

        # give it a moment, then kill if still alive
        self.root.after(2500, self._kill_if_needed)

    def restart_bot(self) -> None:
        with self._proc_lock:
            running = self.proc is not None and self.proc.poll() is None

        if running:
            self.stop_bot()
            # start after stop attempts
            self.root.after(3000, self.start_bot)
        else:
            self.start_bot()

    def _kill_if_needed(self) -> None:
        with self._proc_lock:
            p = self.proc
        if not p:
            return
        if p.poll() is None:
            self._append_line("[controller] Bot did not exit; killing...")
            try:
                p.kill()
            except Exception:
                pass

    def _run_pip_install(self) -> int:
        req_path = os.path.join(self.root_dir, REQ_FILE)
        if not os.path.exists(req_path):
            self.q.put(("line", f"[controller] Skipping pip install: missing {REQ_FILE}"))
            return 0

        # Run synchronously, but keep it short and visible.
        cmd = [sys.executable, "-m", "pip", "install", "-r", REQ_FILE]
        try:
            p = subprocess.Popen(
                cmd,
                cwd=self.root_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert p.stdout is not None
            for line in p.stdout:
                self.q.put(("line", line.rstrip("\n")))
            rc = p.wait()
            self.q.put(("line", f"[controller] pip exit code: {rc}"))
            return rc
        except Exception as e:
            self.q.put(("line", f"[controller] pip failed: {e!r}"))
            return 1

    def _start_sequence(self) -> None:
        """Background worker: optional pip install then start the bot subprocess."""
        if self.pip_on_start_var.get():
            self.q.put(("line", "[controller] Installing/upgrading requirements..."))
            rc = self._run_pip_install()
            if rc != 0:
                self.q.put(("line", "[controller] Requirements install failed; bot not started."))
                self.q.put(("event", "start_failed"))
                return

        self.q.put(("line", "[controller] Starting bot..."))

        cmd = [sys.executable, "-u", BOT_SCRIPT]

        creationflags = 0
        if is_windows():
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        try:
            p = subprocess.Popen(
                cmd,
                cwd=self.root_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=creationflags,
            )
        except Exception as e:
            self.q.put(("line", f"ERROR: Failed to start bot: {e!r}"))
            self.q.put(("event", "start_failed"))
            return

        with self._proc_lock:
            self.proc = p

        self.q.put(("event", "started"))

        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

    # ---------------- Output handling ----------------

    def _reader_loop(self) -> None:
        with self._proc_lock:
            p = self.proc
        if p is None:
            self.q.put(("event", "proc_missing"))
            return

        out = p.stdout
        if out is None:
            self.q.put(("event", "proc_stdout_missing"))
            return

        for raw in out:
            line = raw.rstrip("\n")
            self.q.put(("line", line))

        rc = p.wait()
        self.q.put(("event", f"exit:{rc}"))

    def _pump_queue(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()

                if kind == "line":
                    self._append_line(payload)

                    # "Connected" heuristic: discord.py ready log line
                    if "Logged in as" in payload:
                        self.connected = True
                        self._set_status("Connected")

                elif kind == "event":
                    if payload == "started":
                        self._starting = False
                        self._set_status("Running")
                        self._refresh_buttons()
                        continue

                    if payload == "start_failed":
                        self._starting = False
                        with self._proc_lock:
                            self.proc = None
                        self.connected = False
                        self._set_status("Stopped")
                        self._refresh_buttons()
                        continue

                    if payload.startswith("exit:"):
                        try:
                            rc = int(payload.split(":", 1)[1])
                        except Exception:
                            rc = -1
                        self._append_line(f"[controller] Bot exited with code {rc}")

                    with self._proc_lock:
                        self.proc = None
                    self._starting = False
                    self.connected = False
                    self._set_status("Stopped")
                    self._refresh_buttons()

        except queue.Empty:
            pass

        self.root.after(100, self._pump_queue)

    # ---------------- Small helpers ----------------

    def _append_line(self, s: str) -> None:
        # keep UI responsive; don't do fancy formatting
        if not s:
            return

        uptime = ""
        with self._proc_lock:
            running = self.proc is not None and self.proc.poll() is None

        if self.start_ts is not None and running:
            dt = int(time.time() - self.start_ts)
            uptime = f" (uptime {dt}s)"

        if s.startswith("[controller]"):
            self.detail_var.set(s.replace("[controller]", "controller").strip() + uptime)

        self.log_text.insert("end", s + "\n")
        self.log_text.see("end")

    def clear_logs(self) -> None:
        self.log_text.delete("1.0", "end")

    def _set_status(self, s: str) -> None:
        self.status_var.set(s)
        if s == "Connected":
            self.detail_var.set("Bot is online and ready")
        elif s == "Running":
            self.detail_var.set("Bot process is running")
        else:
            self.detail_var.set("")

    def _refresh_buttons(self) -> None:
        with self._proc_lock:
            running = self.proc is not None and self.proc.poll() is None
        starting = self._starting
        self.start_btn.configure(state=("disabled" if (running or starting) else "normal"))
        self.stop_btn.configure(state=("normal" if running else "disabled"))
        self.restart_btn.configure(state=("normal" if running else "disabled"))

    def _on_close(self) -> None:
        with self._proc_lock:
            running = self.proc is not None and self.proc.poll() is None

        if running:
            res = messagebox.askyesnocancel(
                APP_TITLE,
                "Bot is still running.\n\nYes: stop bot and exit\nNo: leave bot running and exit\nCancel: keep controller open",
            )
            if res is None:
                return
            if res is True:
                self.stop_bot()
                # let stop sequence kick in
                self.root.after(600, self.root.destroy)
                return

        self.root.destroy()


def main() -> None:
    # Helps Windows show a distinct taskbar identity/icon instead of grouping under python.exe.
    try_set_windows_app_user_model_id("scales.bot.controller")

    root = tk.Tk()

    # A little nicer default padding/appearance
    try:
        style = ttk.Style()
        if is_windows():
            style.theme_use("vista")
        else:
            style.theme_use("clam")
    except Exception:
        pass

    BotControllerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
