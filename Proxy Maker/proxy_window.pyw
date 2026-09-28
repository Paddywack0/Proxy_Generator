"""
proxy_window.pyw  -  Step 2: drag-and-drop window for make_proxies.py

Double-click to open (the .pyw ending means no black Command Prompt box).
Keep this file in the SAME folder as make_proxies.py - it uses that
script's tested code to do the actual work.

HOW IT'S ORGANISED:
  - The window (buttons, list, progress bars) runs on the "main thread".
  - The proxy-making runs on a separate "worker thread", so the window
    never freezes while ffmpeg is busy.
  - Tkinter windows must only be changed from the main thread, so the
    worker never touches the window directly. Instead it drops messages
    into a queue (like a pigeonhole), and the window checks that queue
    ten times a second and updates itself.
"""

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import make_proxies as core

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DRAG_DROP = True
except ImportError:
    DRAG_DROP = False


def format_time(seconds):
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class ProxyWindow:
    def __init__(self, root):
        self.root = root
        root.title("DNxHR LB Proxy Maker")
        root.geometry("900x620")
        root.minsize(700, 480)

        self.items = []               # one dict per clip in the queue
        self.messages = queue.Queue() # worker -> window pigeonhole
        self.worker = None
        self.stop_after_clip = threading.Event()
        self.stop_now = threading.Event()

        self.build_layout()
        core.LOG_HOOK = lambda line: self.messages.put(("log", line))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.check_messages)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def build_layout(self):
        pad = {"padx": 8, "pady": 4}

        # Drop zone + add buttons
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        drop_text = ("Drag shoot folders or clips here" if DRAG_DROP
                     else "Drag-and-drop unavailable - use 'Add folder…'")
        self.drop_zone = tk.Label(top, text=drop_text, relief="groove",
                                  height=3, bg="#eef3f8", fg="#335")
        self.drop_zone.pack(side="left", fill="x", expand=True)
        buttons = ttk.Frame(top)
        buttons.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Add folder…", command=self.add_folder_dialog).pack(fill="x")
        ttk.Button(buttons, text="Clear finished", command=self.clear_finished).pack(fill="x", pady=(4, 0))

        if DRAG_DROP:
            self.drop_zone.drop_target_register(DND_FILES)
            self.drop_zone.dnd_bind("<<Drop>>", self.on_drop)

        # Queue list
        columns = ("clip", "folder", "length", "status")
        self.tree = ttk.Treeview(self.root, columns=columns, show="headings", height=10)
        for col, title, width in [("clip", "Clip", 220), ("folder", "Folder", 420),
                                  ("length", "Length", 70), ("status", "Status", 110)]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, **pad)

        # Progress bars
        prog = ttk.Frame(self.root)
        prog.pack(fill="x", **pad)
        self.clip_label = ttk.Label(prog, text="Current clip: -")
        self.clip_label.grid(row=0, column=0, sticky="w")
        self.clip_bar = ttk.Progressbar(prog, maximum=1.0)
        self.clip_bar.grid(row=1, column=0, sticky="ew")
        self.total_label = ttk.Label(prog, text="Whole queue: -")
        self.total_label.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.total_bar = ttk.Progressbar(prog, maximum=1.0)
        self.total_bar.grid(row=3, column=0, sticky="ew")
        prog.columnconfigure(0, weight=1)

        # Start / Stop buttons
        controls = ttk.Frame(self.root)
        controls.pack(fill="x", **pad)
        self.start_btn = ttk.Button(controls, text="Start", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_clip_btn = ttk.Button(controls, text="Stop after this clip",
                                        command=self.request_stop_after_clip, state="disabled")
        self.stop_clip_btn.pack(side="left", padx=6)
        self.stop_now_btn = ttk.Button(controls, text="Stop now",
                                       command=self.request_stop_now, state="disabled")
        self.stop_now_btn.pack(side="left")

        # Log panel
        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill="both", expand=False, **pad)
        self.log_box = tk.Text(log_frame, height=8, wrap="none", state="disabled",
                               font=("Consolas", 9))
        scroll = ttk.Scrollbar(log_frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scroll.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

    # ------------------------------------------------------------------
    # Adding clips
    # ------------------------------------------------------------------
    def on_drop(self, event):
        # splitlist copes with paths containing spaces (Windows wraps them in {})
        for path in self.root.tk.splitlist(event.data):
            self.add_path(path)

    def add_folder_dialog(self):
        folder = filedialog.askdirectory(title="Choose a shoot folder")
        if folder:
            self.add_path(folder)

    def add_path(self, path):
        path = Path(path)
        # The log file goes in the folder you dropped (or the clip's folder).
        log_folder = path if path.is_dir() else path.parent
        already = {item["path"] for item in self.items}
        self.drop_zone.config(text="Scanning…")
        self.root.update_idletasks()

        added = 0
        for clip in core.find_clips(path):
            if clip in already:
                continue
            try:
                length = float(core.probe(clip)["format"]["duration"])
            except Exception:
                length = 0.0
            iid = self.tree.insert("", "end", values=(clip.name, str(clip.parent),
                                                      format_time(length), "waiting"))
            self.items.append({"path": clip, "log_folder": log_folder,
                               "length": length, "status": "waiting", "iid": iid})
            added += 1

        self.drop_zone.config(text="Drag shoot folders or clips here" if DRAG_DROP
                              else "Drag-and-drop unavailable - use 'Add folder…'")
        self.write_log(f"Added {added} clip(s) from {path}")
        self.update_total(0.0, 0.0)

    def clear_finished(self):
        if self.worker and self.worker.is_alive():
            return
        keep = []
        for item in self.items:
            if item["status"] in ("done", "skipped"):
                self.tree.delete(item["iid"])
            else:
                keep.append(item)
        self.items = keep
        self.update_total(0.0, 0.0)

    # ------------------------------------------------------------------
    # Starting and stopping
    # ------------------------------------------------------------------
    def start(self):
        todo = [i for i in self.items if i["status"] in ("waiting", "stopped", "failed")]
        if not todo:
            messagebox.showinfo("Nothing to do", "Add some clips first.")
            return
        self.stop_after_clip.clear()
        self.stop_now.clear()
        self.set_running(True)
        self.worker = threading.Thread(target=self.work, args=(todo,), daemon=True)
        self.worker.start()

    def request_stop_after_clip(self):
        self.stop_after_clip.set()
        self.write_log("Will stop when the current clip finishes…")
        self.stop_clip_btn.config(state="disabled")

    def request_stop_now(self):
        self.stop_now.set()
        self.write_log("Stopping now…")

    def set_running(self, running):
        self.start_btn.config(state="disabled" if running else "normal")
        self.stop_clip_btn.config(state="normal" if running else "disabled")
        self.stop_now_btn.config(state="normal" if running else "disabled")

    # ------------------------------------------------------------------
    # The worker thread - does the actual proxy making
    # ------------------------------------------------------------------
    def work(self, todo):
        for item in todo:
            if self.stop_after_clip.is_set() or self.stop_now.is_set():
                break
            self.messages.put(("status", item, "working"))
            core.LOG_FILE = item["log_folder"] / "proxy_log.txt"

            def progress(fraction, fps, speed, item=item):
                self.messages.put(("progress", item, fraction, fps, speed))

            try:
                result = core.make_proxy(item["path"], on_progress=progress,
                                         stop_now=self.stop_now)
            except Exception as error:
                core.log(f"FAILED {item['path'].name} ({error})")
                result = "failed"
            self.messages.put(("status", item, result))
        self.messages.put(("finished",))

    # ------------------------------------------------------------------
    # Window side: read the pigeonhole and update the display
    # ------------------------------------------------------------------
    def check_messages(self):
        try:
            while True:
                msg = self.messages.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.write_log(msg[1])
                elif kind == "status":
                    _, item, status = msg
                    item["status"] = status
                    item.pop("fraction", None)
                    self.tree.set(item["iid"], "status", status)
                    self.tree.see(item["iid"])
                    if status == "working":
                        self.clip_label.config(text=f"Current clip: {item['path'].name}")
                        self.clip_bar["value"] = 0
                    self.update_total(0.0, 0.0)
                elif kind == "progress":
                    _, item, fraction, fps, speed = msg
                    item["fraction"] = fraction
                    self.clip_bar["value"] = fraction
                    self.tree.set(item["iid"], "status", f"working {fraction:.0%}")
                    self.clip_label.config(
                        text=f"Current clip: {item['path'].name}   "
                             f"{fps:.0f} fps   {speed:.2f}x realtime")
                    self.update_total(fraction, speed)
                elif kind == "finished":
                    self.set_running(False)
                    self.clip_label.config(text="Current clip: -")
                    self.clip_bar["value"] = 0
                    self.write_log("Queue stopped." if (self.stop_now.is_set() or
                                   self.stop_after_clip.is_set()) else "Queue finished.")
        except queue.Empty:
            pass
        self.root.after(100, self.check_messages)

    def update_total(self, current_fraction, speed):
        """Overall progress measured in seconds of footage, not number of clips."""
        total = sum(i["length"] for i in self.items) or 1
        done = sum(i["length"] for i in self.items if i["status"] in ("done", "skipped"))
        done += sum(i["length"] * i.get("fraction", 0)
                    for i in self.items if i["status"] == "working")
        self.total_bar["value"] = done / total
        remaining = sum(i["length"] for i in self.items
                        if i["status"] in ("waiting", "working", "stopped")) \
            - sum(i["length"] * i.get("fraction", 0)
                  for i in self.items if i["status"] == "working")
        eta = f"   about {format_time(remaining / speed)} left" if speed > 0 else ""
        counts = {s: sum(1 for i in self.items if i["status"] == s)
                  for s in ("done", "skipped", "failed")}
        self.total_label.config(
            text=f"Whole queue: {done / total:.0%}   {counts['done']} done, "
                 f"{counts['skipped']} skipped, {counts['failed']} failed{eta}")

    def write_log(self, line):
        self.log_box.config(state="normal")
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Still working",
                                       "Proxies are still being made. Stop and quit?"):
                return
            self.stop_now.set()
            self.worker.join(timeout=10)
        self.root.destroy()


if __name__ == "__main__":
    root = TkinterDnD.Tk() if DRAG_DROP else tk.Tk()
    ProxyWindow(root)
    root.mainloop()
