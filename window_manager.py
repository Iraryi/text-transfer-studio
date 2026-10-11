"""Restore modal windows as one application group, without stealing external focus."""
import ctypes
import os
import tkinter as tk


class ModalWindows:
    def __init__(self,root):
        self.root=root;self.stack=[];self.pending=None;self.want_focus=False;self.closed=False;self.restoring=False
        self.watch(root)
        root.bind('<Destroy>',self.root_destroyed,add='+')

    def watch(self,window):
        window.bind('<Map>',lambda event:self.mapped(event,window),add='+')
        window.bind('<Unmap>',lambda event:self.unmapped(event,window),add='+')
        window.bind('<FocusIn>',lambda event:self.activated(window),add='+')

    def open(self,window):
        # All native transient windows belong directly to the root. Nested native
        # owners can remain hidden after the taskbar restores only their ancestor.
        window.transient(self.root)
        self.stack.append(window);self.watch(window)
        window.bind('<Destroy>',lambda event:self.destroyed(event,window),add='+')
        self.schedule(True)

    def schedule(self,focus=False):
        if self.closed:return
        self.want_focus=self.want_focus or focus
        if self.pending is None:self.pending=self.root.after_idle(self.restore)

    def mapped(self,event,window):
        if event.widget==window and not self.restoring:self.schedule()

    def unmapped(self,event,window):
        if event.widget!=window or self.closed:return
        # A hidden modal must not keep capturing clicks intended to restore its owner.
        try:
            grabbed=self.root.grab_current()
            if grabbed in self.stack:grabbed.grab_release()
        except (tk.TclError,KeyError):pass

    def activated(self,window):
        if self.closed or self.restoring or not self.stack:return
        try:
            focused=self.root.focus_get()
            if focused is not None and focused.winfo_toplevel()==self.stack[-1]:return
        except (tk.TclError,KeyError):return
        if window!=self.stack[-1]:self.schedule(True)

    def foreground_is_ours(self):
        if os.name!='nt':return self.root.focus_displayof() is not None
        user=ctypes.windll.user32
        user.GetForegroundWindow.restype=ctypes.c_void_p
        user.GetWindowThreadProcessId.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
        pid=ctypes.c_ulong();user.GetWindowThreadProcessId(user.GetForegroundWindow(),ctypes.byref(pid))
        return pid.value==os.getpid()

    def restore(self):
        self.pending=None;focus=self.want_focus;self.want_focus=False
        if self.closed or self.restoring:return
        self.restoring=True
        try:
            if self.root.state() in ('iconic','iconified','withdrawn'):return
            self.stack=[window for window in self.stack if window.winfo_exists()]
            if not self.stack:return
            for window in self.stack:
                if window.state()!='normal' or not window.winfo_viewable():window.deiconify()
                if os.name=='nt':
                    # Native owner-hiding does not always change Tk's reported state.
                    user=ctypes.windll.user32
                    user.GetParent.argtypes=[ctypes.c_void_p];user.GetParent.restype=ctypes.c_void_p
                    user.ShowWindow.argtypes=[ctypes.c_void_p,ctypes.c_int]
                    user.IsWindowVisible.argtypes=[ctypes.c_void_p]
                    handle=user.GetParent(window.winfo_id())
                    if not user.IsWindowVisible(handle):user.ShowWindow(handle,4)  # no activation
            top=self.stack[-1]
            grabbed=self.root.grab_current()
            # Respect native Tk message boxes/file dialogs while they own the grab.
            if grabbed is not None and grabbed not in self.stack:return
            if top.winfo_viewable():top.grab_set()
            focused=self.root.focus_get()
            already_focused=focused is not None and focused.winfo_toplevel()==top
            if self.foreground_is_ours() and not already_focused:
                top.lift()
                if focus:top.focus_set()
        except (tk.TclError,KeyError):pass
        finally:self.restoring=False

    def dismiss(self,window):
        if self.closed:return
        try:
            if self.root.grab_current()==window:window.grab_release()
        except (tk.TclError,KeyError):pass
        if window in self.stack:self.stack.remove(window)
        self.schedule(True)

    def destroyed(self,event,window):
        if event.widget==window:self.dismiss(window)

    def root_destroyed(self,event):
        if event.widget!=self.root:return
        self.shutdown()

    def shutdown(self):
        self.closed=True
        if self.pending:
            try:self.root.after_cancel(self.pending)
            except tk.TclError:pass
        self.pending=None;self.stack=[]
