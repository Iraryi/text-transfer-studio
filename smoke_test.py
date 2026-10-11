"""Explicit --self-test-dir harness; neutral fixture, no network or API credentials."""
import ctypes
import json
import time
from pathlib import Path
from PIL import ImageGrab
from tkinter import ttk
if __package__:
    from . import model as m, imaging, style_match
else:
    import model as m
    import imaging
    import style_match


def capture(window,path):
    window.update_idletasks()
    hwnd=ctypes.windll.user32.GetParent(window.winfo_id())
    ImageGrab.grab(window=hwnd).save(path)


def start(editor,directory):
    directory=Path(directory).resolve();directory.mkdir(parents=True,exist_ok=True)
    # Never overwrite an earlier test report or another person's image.
    if any(directory.iterdir()):raise ValueError('Self-test output directory must be empty.')
    flow=editor.workflow_window;began=time.monotonic();errors=[];raw_ocr=[];corrections=[]
    assert not flow.auto_style.get() and flow.style_sensitivity.get()==0
    # Explicit opt-in tests the high-sensitivity matcher, not release defaults.
    flow.auto_style.set(True);flow.style_sensitivity.set(100)
    def error(text):errors.append(text)
    flow.error=error
    def finish(status):
        report={'status':status,'errors':errors,'ocr':[r['source'] for r in flow.doc.records],
                'seconds':round(time.monotonic()-began,2),'network_requests':0,
                'ai_live_tested':False,'checks':['OCR mixed CJK/English','mask generation','repair candidate accept',
                'per-line font/size/weight/color estimation','alpha/outside-mask checks','editable text generation','A-to-B transfer','project roundtrip','PNG export',
                'native minimize/restore with AI settings modal','unsaved settings draft preserved','settings close returns workflow grab'],
                'raw_ocr':raw_ocr,'simulated_manual_corrections':corrections}
        (directory/'smoke-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        flow.closed=True
        editor.modals.shutdown()
        if flow.timer:flow.win.after_cancel(flow.timer)
        if editor.pending:editor.root.after_cancel(editor.pending)
        editor.root.destroy()
    def run_checks():
        if errors:return finish('FAILED')
        if flow.busy:
            if time.monotonic()-began>90:errors.append('OCR timeout');return finish('FAILED')
            return editor.root.after(200,run_checks)
        try:
            # Exercise native minimize/restore with a second-level modal and unsaved input.
            dialog=flow.settings();editor.root.update()
            entries=[w for w in dialog.winfo_children()[0].winfo_children() if isinstance(w,ttk.Entry)]
            entries[1].insert(0,'FAKE-SMOKE-DRAFT-NOT-A-KEY')
            editor.root.iconify();editor.root.update();assert editor.root.grab_current() is None
            editor.root.deiconify();editor.root.update()
            assert dialog.winfo_viewable() and flow.win.winfo_viewable()
            assert editor.root.grab_current()==dialog
            assert entries[1].get()=='FAKE-SMOKE-DRAFT-NOT-A-KEY' and editor.ai_config.key==''
            capture(dialog,directory/'ai-restored-window.png')
            editor.root.tk.call(dialog.protocol('WM_DELETE_WINDOW'));editor.root.update()
            assert editor.root.grab_current()==flow.win
            raw_ocr.extend(r['source'] for r in flow.doc.records)
            assert [r['source'].rstrip('。') for r in flow.doc.records]==['城市植物观察日记','今日は図書館で本を読みます','TEXT SHIFT 2026']
            if flow.doc.records[1]['source']!='今日は図書館で本を読みます。':
                corrections.append('Japanese sentence-ending punctuation restored via review text field')
                flow.table.selection_set(flow.doc.records[1]['id']);flow.select();flow.source.delete('1.0','end');flow.source.insert('1.0','今日は図書館で本を読みます。');flow.save_row()
                row=flow.doc.records[1];xs=[p[0]*flow.doc.image.width for p in row['box']];ys=[p[1]*flow.doc.image.height for p in row['box']]
                flow.roi=(int(min(xs)),int(min(ys)),min(flow.doc.image.width,int(max(xs))+16),int(max(ys))+1);flow.rebox();flow.roi=None
                corrections.append('OCR box expanded in review to include missed punctuation before text masking')
                flow.doc.records=style_match.estimate_rows(flow.doc.original,flow.doc.records)
                flow.table.selection_remove(*flow.table.selection());flow.refresh()
            assert [r['style']['color'] for r in flow.doc.records]==['#96352f','#354e85','#316b4f']
            assert flow.doc.records[0]['style']['size']>flow.doc.records[1]['style']['size']>flow.doc.records[2]['style']['size']
            assert Path(flow.doc.records[0]['style']['font']).name.lower()=='msyhbd.ttc'
            capture(flow.win,directory/'ocr-window.png')
            (directory/'line-styles.json').write_text(json.dumps(flow.doc.records,ensure_ascii=False,indent=2),encoding='utf-8')
            flow.generate_mask();flow.notebook.select(1);flow.win.update();capture(flow.win,directory/'mask-window.png')
            flow.show_candidate(imaging.repair(flow.doc.image,flow.doc.mask,'solid',color='#ffffff'));flow.accept()
            assert flow.doc.image.getchannel('A').tobytes()==flow.doc.original.getchannel('A').tobytes()
            import numpy as np
            outside=np.asarray(flow.doc.mask)==0
            assert np.array_equal(np.asarray(flow.doc.image)[outside],np.asarray(flow.doc.original)[outside])
            # The neutral white card should be truly blank after reviewed masking.
            assert np.all(np.asarray(flow.doc.image)[:,:,:3]==255),'Text removal left visible pixels on neutral test card'
            for row,text in zip(flow.doc.records,['城市植物观察日记','今天在图书馆读书。','文字迁移 2026']):row['target']=text
            flow.make_layers();assert len(flow.doc.layers)==3
            flow.view.set('合成');flow.draw();capture(flow.win,directory/'restored-window.png')
            flow.doc.image.save(directory/'restored-background.png');flow.doc.mask.save(directory/'mask.png')
            flow.doc.original.save(directory/'original.png');m.render(flow.doc).save(directory/'composite-A.png')
            flow.close();editor.transfer_scope.set('全部文字层');editor.transfer(0);assert len(editor.docs[1].layers)==3
            m.render(editor.docs[1]).save(directory/'composite-B.png')
            m.save_project(directory/'roundtrip.textshift',editor.docs);docs,warnings=m.load_project(directory/'roundtrip.textshift')
            assert not warnings and len(docs[0].records)==3 and len(docs[1].layers)==3
            assert m.render(docs[0]).tobytes()==m.render(editor.docs[0]).tobytes()
            editor.root.update();capture(editor.root,directory/'transfer-window.png')
            finish('PASSED')
        except Exception as exc:
            errors.append(type(exc).__name__+': '+str(exc));finish('FAILED')
    editor.root.after(600,lambda:flow.ocr(False))
    editor.root.after(900,run_checks)
