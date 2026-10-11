"""Guided OCR/review/restoration workspace, sharing the editor's undo history."""
import copy
import json
import queue
import threading
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, colorchooser, simpledialog
from PIL import Image, ImageDraw, ImageTk, ImageChops, ImageEnhance
if __package__:
    from . import model as m, imaging, ai_client, style_match
else:
    import model as m
    import imaging
    import ai_client
    import style_match


class Workflow:
    def __init__(self,editor,index):
        self.editor=editor;self.index=index;self.doc=editor.docs[index]
        self.win=tk.Toplevel(editor.root);self.win.title('字迁 TextShift — OCR · 校对 · 背景修复 / '+'AB'[index])
        self.win.geometry('1380x880');self.win.minsize(1060,720);self.ai_dialog=None
        self.closed=False;self.busy=False;self.job_id=0;self.results=queue.Queue();self.timer=None
        self.start=None;self.stroke=None;self.candidate=None;self.roi=None;self.selected=None
        self.clone_source=None;self.clone_base=None;self.fill_color='#ffffff'
        self.residuals=[]
        self.scale=1;self.zoom=tk.StringVar(value='适应');self.mode=tk.StringVar(value='框选 OCR')
        self.view=tk.StringVar(value='蒙版');self.brush=tk.StringVar(value='12');self.threshold=tk.StringVar(value='150')
        self.dilation=tk.StringVar(value='2');self.radius=tk.StringVar(value='3');self.mask_mode=tk.StringVar(value='深色文字')
        self.context=tk.StringVar();self.language=tk.StringVar(value='简体中文')
        self.auto_style=editor.auto_style;self.style_sensitivity=editor.style_sensitivity
        self.style_note=tk.StringVar(value='尚无逐行样式估计。')
        self.note=tk.StringVar(value='先识别，再校对；红色区域是待修复蒙版。所有修改可撤销，原图文件不改动。')
        self.build();self.refresh();self.timer=self.win.after(100,self.poll)
        self.win.protocol('WM_DELETE_WINDOW',self.close)
        self.editor.modals.open(self.win)

    def build(self):
        top=ttk.Frame(self.win,padding=12);top.pack(fill='x')
        ttk.Label(top,text='识字与修图',font=('Microsoft YaHei UI',18,'bold')).pack(side='left')
        for label,cmd in [('返回排版',self.close),('检查报告',self.inspect),('AI 设置',self.settings),('重做',lambda:self.undo(True)),('撤销',self.undo)]:
            ttk.Button(top,text=label,command=cmd).pack(side='right',padx=3)
        body=ttk.Frame(self.win,padding=(12,0));body.pack(fill='both',expand=True)
        side=ttk.Frame(body,width=440);side.pack(side='right',fill='both',padx=(10,0));side.pack_propagate(False)
        left=ttk.Frame(body);left.pack(side='left',fill='both',expand=True)
        row=ttk.Frame(left);row.pack(fill='x',pady=5)
        for title,var,values,width in [('查看',self.view,['蒙版','原图','修复底图','合成','差异','候选'],10),('缩放',self.zoom,['适应','50%','100%','200%','400%'],7)]:
            ttk.Label(row,text=title).pack(side='left',padx=3)
            combo=ttk.Combobox(row,textvariable=var,values=values,state='readonly',width=width);combo.pack(side='left');combo.bind('<<ComboboxSelected>>',lambda e:self.draw())
        ttk.Button(row,text='导出当前视图',command=self.export_view).pack(side='right')
        frame=ttk.Frame(left);frame.pack(fill='both',expand=True);frame.columnconfigure(0,weight=1);frame.rowconfigure(0,weight=1)
        self.canvas=tk.Canvas(frame,bg='#dfe6e1',highlightthickness=0,cursor='crosshair')
        self.canvas.grid(row=0,column=0,sticky='nsew')
        h=ttk.Scrollbar(frame,orient='horizontal',command=lambda *args:self.scroll('x',*args));h.grid(row=1,column=0,sticky='ew')
        v=ttk.Scrollbar(frame,orient='vertical',command=lambda *args:self.scroll('y',*args));v.grid(row=0,column=1,sticky='ns')
        self.canvas.configure(xscrollcommand=h.set,yscrollcommand=v.set)
        self.canvas.bind('<Configure>',lambda e:self.draw())
        self.canvas.bind('<ButtonPress-1>',self.press);self.canvas.bind('<B1-Motion>',self.motion);self.canvas.bind('<ButtonRelease-1>',self.release)
        self.canvas.bind('<Alt-ButtonPress-1>',self.sample)
        self.canvas.bind('<ButtonPress-2>',lambda e:self.canvas.scan_mark(e.x,e.y));self.canvas.bind('<B2-Motion>',self.pan)
        row=ttk.Frame(left);row.pack(fill='x',pady=6)
        ttk.Combobox(row,textvariable=self.mode,values=['框选 OCR','矩形蒙版','蒙版画笔','蒙版橡皮','仿制画笔'],state='readonly',width=12).pack(side='left')
        ttk.Label(row,text='笔刷直径（原图像素）').pack(side='left',padx=5)
        ttk.Spinbox(row,from_=1,to=200,textvariable=self.brush,width=5).pack(side='left')
        ttk.Button(row,text='清空蒙版',command=self.clear_mask).pack(side='right')
        ttk.Label(left,text='中键拖动画布；Alt + 单击取色 / 设置仿制源点。仿制画笔直接修改底图，可撤销。其他修复仅作用于蒙版。',wraplength=650).pack(anchor='w',pady=5)
        notebook=ttk.Notebook(side);notebook.pack(fill='both',expand=True);self.notebook=notebook
        def tab(title):
            outer=ttk.Frame(notebook);notebook.add(outer,text=title)
            canvas=tk.Canvas(outer,highlightthickness=0,bg='#eef2ef')
            scrollbar=ttk.Scrollbar(outer,orient='vertical',command=canvas.yview);scrollbar.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
            canvas.configure(yscrollcommand=scrollbar.set);inner=ttk.Frame(canvas,padding=8)
            item=canvas.create_window(0,0,window=inner,anchor='nw')
            inner.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
            canvas.bind('<Configure>',lambda e:canvas.itemconfigure(item,width=e.width))
            def wheel(event):
                if str(event.widget).startswith(str(inner)) and not isinstance(event.widget,(tk.Text,ttk.Treeview,ttk.Combobox,ttk.Spinbox)):
                    canvas.yview_scroll(-int(event.delta/120),'units');return 'break'
            self.win.bind('<MouseWheel>',wheel,add='+')
            return inner
        review=tab('1 识别与校对');repair=tab('2 去字与修复')
        row=ttk.Frame(review);row.pack(fill='x')
        for label,cmd in [('整图 OCR',lambda:self.ocr(False)),('选区 OCR',lambda:self.ocr(True)),('手动加行',self.add_row)]:ttk.Button(row,text=label,command=cmd).pack(side='left',padx=2)
        ttk.Label(review,text='本地识别 · 中 / 日 / 英混排；自动结果必须人工校对',wraplength=400).pack(anchor='w',pady=7)
        ttk.Checkbutton(review,text='启用自动字形匹配（关闭后不套用已有估计）',variable=self.auto_style).pack(anchor='w')
        ttk.Label(review,text='敏感度：0 最保守，100 最宽松；默认 0').pack(anchor='w')
        ttk.Scale(review,from_=0,to=100,variable=self.style_sensitivity).pack(fill='x')
        ttk.Button(review,text='重新估计选中 / 全部行的原字样式',command=self.estimate_style).pack(fill='x',pady=4)
        self.table=ttk.Treeview(review,columns=('score','source','target'),show='headings',height=8,selectmode='extended')
        for key,title,width in [('score','置信度',52),('source','原文',150),('target','译文',150)]:
            self.table.heading(key,text=title);self.table.column(key,width=width,minwidth=30)
        self.table.pack(fill='both',expand=True);self.table.bind('<<TreeviewSelect>>',self.select)
        self.row_menu=tk.Menu(self.win,tearoff=False)
        for label,cmd in [('本行上移',lambda:self.reorder(-1)),('本行下移',lambda:self.reorder(1)),('把本行位置改为当前选区',self.rebox),('合并选中行',self.merge_rows),('取消行选择（后续操作作用于全部）',lambda:self.table.selection_remove(*self.table.selection()))]:self.row_menu.add_command(label=label,command=cmd)
        self.table.bind('<Button-3>',lambda e:self.row_menu.tk_popup(e.x_root,e.y_root))
        bar=ttk.Frame(review);bar.pack(fill='x',pady=4)
        for label,cmd in [('删除行',self.delete_rows),('导入 JSON',self.import_rows),('导出 JSON',self.export_rows)]:ttk.Button(bar,text=label,command=cmd).pack(side='left',padx=2)
        ttk.Label(review,text='右键可排序 / 合并 / 改框；Ctrl 点选多行。').pack(anchor='w')
        ttk.Label(review,text='原文校正（点击一行后编辑，按“保存本行”）').pack(anchor='w')
        self.source=tk.Text(review,height=3,wrap='word',font=('Microsoft YaHei UI',10));self.source.pack(fill='x')
        ttk.Label(review,text='译文 / 替换文字').pack(anchor='w',pady=(5,0))
        self.target=tk.Text(review,height=3,wrap='word',font=('Microsoft YaHei UI',10));self.target.pack(fill='x')
        ttk.Label(review,textvariable=self.style_note,wraplength=370,foreground='#577368').pack(anchor='w',pady=4)
        ttk.Button(review,text='保存本行',command=self.save_row).pack(fill='x',pady=4)
        ttk.Label(review,text='翻译上下文（人物称谓、术语、场景等，可选）').pack(anchor='w')
        ttk.Entry(review,textvariable=self.context).pack(fill='x')
        row=ttk.Frame(review);row.pack(fill='x',pady=4)
        ttk.Label(row,text='目标语言').pack(side='left');ttk.Entry(row,textvariable=self.language,width=10).pack(side='left',padx=4)
        ttk.Button(row,text='AI 翻译选中 / 全部行',command=self.translate).pack(side='right')
        ttk.Label(review,text='生成时继承每行估计样式；长译文会缩小字号以适应原框。\n字体是候选近似匹配，不保证原字体。返回排版可逐行调整。',wraplength=380).pack(anchor='w',pady=5)
        ttk.Button(review,text='选中 / 全部行 → 可编辑文字层',command=self.make_layers,style='Accent.TButton').pack(fill='x')
        ttk.Label(repair,text='先从识别框生成蒙版，再用画笔补全、橡皮保护细节。',wraplength=390).pack(anchor='w',pady=6)
        ttk.Combobox(repair,textvariable=self.mask_mode,values=['深色文字','浅色文字','整框'],state='readonly').pack(fill='x')
        for title,var,start,end in [('明暗阈值',self.threshold,0,255),('蒙版扩张 / px',self.dilation,0,30),('修复半径 / px',self.radius,1,30)]:
            row=ttk.Frame(repair);row.pack(fill='x',pady=6);ttk.Label(row,text=title).pack(side='left');ttk.Spinbox(row,from_=start,to=end,textvariable=var,width=7).pack(side='right')
        ttk.Button(repair,text='识别框 → 去字蒙版（选中 / 全部）',command=self.generate_mask).pack(fill='x',pady=6)
        ttk.Separator(repair).pack(fill='x',pady=10)
        ttk.Label(repair,text='离线修复：适合小字、平面和简单纹理。复杂纹理可能模糊，先看候选再采用。',wraplength=390).pack(anchor='w')
        for label,method in [('局部纹理修复 · Telea','telea'),('局部纹理修复 · NS','ns'),('纯色填补…','solid')]:
            ttk.Button(repair,text=label,command=lambda method=method:self.local_repair(method)).pack(fill='x',pady=4)
        ttk.Button(repair,text='AI 背景修复（需配置并确认联网）',command=self.ai_repair).pack(fill='x',pady=10)
        ttk.Button(repair,text='导入外部修复候选 PNG…',command=self.import_candidate).pack(fill='x',pady=4)
        ttk.Label(repair,text='AI 或外部候选必须与原图尺寸相同。只采用蒙版区域；不自动缩放、裁切或修改其他像素。',wraplength=390).pack(anchor='w',pady=8)
        self.candidate_note=tk.StringVar(value='尚无候选。')
        ttk.Label(repair,textvariable=self.candidate_note,wraplength=390).pack(anchor='w')
        ttk.Button(repair,text='采用候选修复',style='Accent.TButton',command=self.accept).pack(fill='x',pady=5)
        ttk.Button(repair,text='丢弃候选',command=self.reject).pack(fill='x',pady=3)
        ttk.Button(repair,text='恢复未修复的原始底图',command=self.restore).pack(fill='x',pady=12)
        ttk.Button(repair,text='导出去字蒙版 PNG',command=self.export_mask).pack(fill='x')
        ttk.Button(repair,text='对修复底图做残字 OCR 复检',command=self.check_residuals).pack(fill='x',pady=8)
        bottom=ttk.Frame(self.win,padding=12);bottom.pack(fill='x')
        ttk.Button(bottom,text='取消等待',command=self.cancel).pack(side='right')
        ttk.Label(bottom,textvariable=self.note,wraplength=1120).pack(side='left')

    def guard(self):
        if self.busy:self.note.set('当前任务进行中。可取消等待；取消不保证远端停止处理或停止计费。');return False
        if self.unsaved_row():return self.save_row(check_busy=False)
        return True

    def scroll(self,axis,*args):
        (self.canvas.xview if axis=='x' else self.canvas.yview)(*args);self.draw()

    def pan(self,event):
        self.canvas.scan_dragto(event.x,event.y,gain=1);self.draw()

    def commit(self):
        self.editor.history.push(self.editor.docs);self.editor.changed();self.candidate=None;self.residuals=[]

    def run(self,label,work,done):
        if not self.guard():return
        self.busy=True;self.job_id+=1;job=self.job_id;self.note.set(label+'…可继续查看画面；编辑暂时锁定。')
        self.source.configure(state='disabled');self.target.configure(state='disabled')
        def task():
            try:self.results.put((job,done,work(),None))
            except Exception as exc:self.results.put((job,done,None,str(exc)))
        threading.Thread(target=task,daemon=True).start()

    def poll(self):
        if self.closed:return
        try:
            while True:
                job,done,result,error=self.results.get_nowait()
                if job!=self.job_id:continue
                self.busy=False
                self.source.configure(state='normal');self.target.configure(state='normal')
                if error:self.error(error)
                else:
                    try:done(result)
                    except Exception as exc:self.error(str(exc))
        except queue.Empty:pass
        self.timer=self.win.after(100,self.poll)

    def cancel(self):
        if self.busy:
            self.job_id+=1;self.busy=False;self.note.set('已取消等待并丢弃后续结果；远端请求可能仍在处理或计费。')
            self.source.configure(state='normal');self.target.configure(state='normal')

    def close(self):
        if self.unsaved_row() and not messagebox.askyesno('本行未保存','放弃尚未保存的本行文本编辑？其他已保存修改会保留。',parent=self.win):return
        if self.busy and not messagebox.askyesno('任务进行中','关闭将丢弃任务结果；远端可能仍在计费。确定关闭？',parent=self.win):return
        self.closed=True;self.job_id+=1
        if self.timer:self.win.after_cancel(self.timer)
        self.editor.modals.dismiss(self.win);self.win.destroy();self.editor.refresh()

    def error(self,text):self.note.set(text);messagebox.showerror('操作未完成',text,parent=self.win)

    def rows(self):
        ids=set(self.table.selection())
        return [r for r in self.doc.records if not ids or r['id'] in ids]

    def refresh(self):
        ids=self.table.selection();self.table.delete(*self.table.get_children())
        for row in self.doc.records:self.table.insert('','end',iid=row['id'],values=(f'{row["score"]:.0%}',row['source'],row['target']))
        self.table.selection_set([i for i in ids if self.table.exists(i)])
        self.selected=None;self.source.delete('1.0','end');self.target.delete('1.0','end');self.draw()

    def unsaved_row(self):
        row=next((r for r in self.doc.records if r['id']==self.selected),None)
        return row is not None and (row['source']!=self.source.get('1.0','end-1c') or row['target']!=self.target.get('1.0','end-1c'))

    def select(self,event=None):
        if self.busy:return
        ids=self.table.selection()
        new=ids[0] if ids else None
        if self.selected==new:return
        if self.unsaved_row() and not self.save_row():
            if self.selected:self.table.selection_set(self.selected)
            return
        self.selected=new
        row=next((r for r in self.doc.records if r['id']==self.selected),None)
        self.source.delete('1.0','end');self.target.delete('1.0','end')
        if row:self.source.insert('1.0',row['source']);self.target.insert('1.0',row['target'])
        style=row.get('style',{}) if row else {}
        self.style_note.set((f'原字估计：{Path(style["font"]).name} / {style["size"]} px / {style["color"]}\n同色加粗 {style.get("outline",0)} px；近似分数 {style.get("match_score",0):.2f}（非正确概率）') if 'font' in style else style.get('note','尚无逐行样式估计。'))
        self.draw()

    def save_row(self,check_busy=True):
        if check_busy and self.busy:return False
        row=next((r for r in self.doc.records if r['id']==self.selected),None)
        if not row:return True
        candidate=copy.deepcopy(row);candidate['source']=self.source.get('1.0','end-1c');candidate['target']=self.target.get('1.0','end-1c')
        if candidate['source']!=row['source']:candidate.pop('style',None)
        try:m.validate_records([candidate])
        except Exception as exc:self.error(str(exc));return False
        if candidate!=row:
            self.commit();self.doc.records[self.doc.records.index(row)]=candidate
            self.table.item(candidate['id'],values=(f'{candidate["score"]:.0%}',candidate['source'],candidate['target']))
        self.note.set('本行已保存；原文与译文分别保留。')
        return True

    def reorder(self,delta):
        if not self.guard() or not self.save_row():return
        ids=self.table.selection()
        if len(ids)!=1:return self.error('请选择单行排序。')
        i=next(i for i,r in enumerate(self.doc.records) if r['id']==ids[0]);j=i+delta
        if 0<=j<len(self.doc.records):
            self.commit();self.doc.records[i],self.doc.records[j]=self.doc.records[j],self.doc.records[i];self.refresh()

    def rebox(self):
        if not self.guard() or not self.save_row():return
        if not self.roi or len(self.table.selection())!=1:return self.error('请先框选位置并选择单行。')
        row=next(r for r in self.doc.records if r['id']==self.table.selection()[0]);a,b,c,d=self.roi;w,h=self.doc.image.size
        self.commit();row['box']=[[a/w,b/h],[c/w,b/h],[c/w,d/h],[a/w,d/h]];self.draw()
        row.pop('style',None)

    def merge_rows(self):
        if not self.guard() or not self.save_row():return
        if len(self.table.selection())<2:return self.error('Ctrl + 单击选择至少两行。')
        rows=self.rows();xs=[p[0] for r in rows for p in r['box']];ys=[p[1] for r in rows for p in r['box']]
        row=copy.deepcopy(rows[0]);row['source']='\n'.join(r['source'] for r in rows);row['target']='\n'.join(r['target'] or r['source'] for r in rows) if any(r['target'] for r in rows) else ''
        row['score']=min(r['score'] for r in rows);row['box']=[[min(xs),min(ys)],[max(xs),min(ys)],[max(xs),max(ys)],[min(xs),max(ys)]]
        row.pop('style',None)
        try:m.validate_records([row])
        except Exception as exc:return self.error(str(exc))
        self.commit();ids={r['id'] for r in rows};self.doc.records=[row if r['id']==row['id'] else r for r in self.doc.records if r['id']==row['id'] or r['id'] not in ids];self.refresh()

    def ocr(self,region):
        if not self.guard():return
        if region and not self.roi:return self.error('先在画布拖出 OCR 选区。')
        if self.doc.records and not messagebox.askyesno('重新识别','本次结果会替换当前识别表（包括译文）；可撤销。继续？',parent=self.win):return
        if self.unsaved_row():return self.error('请先保存本行文本。')
        image=self.doc.original;roi=self.roi if region else None
        auto_style=self.auto_style.get();fonts=self.style_fonts()
        def done(rows):
            self.commit();self.doc.records=rows;self.refresh();self.note.set(f'识别完成：{len(rows)} 行。请逐行核对顺序、漏字和低置信度内容。')
        def work():
            rows=imaging.recognize(image,roi)
            return style_match.estimate_rows(image,rows,fonts) if auto_style else rows
        self.run('本地 OCR 识别'+('与逐行样式匹配' if auto_style else ''),work,done)

    def style_fonts(self):
        # Keep matching bounded; explicitly imported fonts join the representative candidates.
        extra=[p for p in self.editor.fonts.values() if Path(p).parent!=Path('C:/Windows/Fonts')]
        return list(dict.fromkeys(style_match.font_candidates()+extra))[:40]

    def estimate_style(self):
        if not self.guard():return
        rows=copy.deepcopy(self.rows());image=self.doc.original;fonts=self.style_fonts()
        if not rows:return self.error('请先 OCR 或手动添加识别行。')
        def done(result):
            self.commit();mapping={r['id']:r for r in result};self.doc.records=[mapping.get(r['id'],r) for r in self.doc.records]
            self.refresh();self.note.set('已生成样式候选。只有开启自动匹配且达到敏感度门槛才会应用；关闭时忽略全部候选。')
        self.run('逐行样式匹配',lambda:style_match.estimate_rows(image,rows,fonts),done)

    def add_row(self):
        if not self.guard():return
        if not self.roi:return self.error('先在画布框选文字位置，再手动加行。')
        if len(self.doc.records)>=1000:return self.error('最多 1000 行。')
        a,b,c,d=self.roi;w,h=self.doc.image.size
        row={'id':uuid.uuid4().hex,'source':'手动输入原文','target':'','score':0.,'box':[[a/w,b/h],[c/w,b/h],[c/w,d/h],[a/w,d/h]]}
        self.commit();self.doc.records.append(row);self.refresh();self.table.selection_set(row['id']);self.select()

    def delete_rows(self):
        if not self.guard():return
        ids=self.table.selection()
        if not ids:return
        self.commit();self.doc.records=[r for r in self.doc.records if r['id'] not in ids];self.refresh()

    def import_rows(self):
        if not self.guard():return
        path=filedialog.askopenfilename(parent=self.win,filetypes=[('识别记录 JSON','*.json')])
        if not path:return
        try:
            if Path(path).stat().st_size>8_000_000:raise ValueError('记录文件过大。')
            rows=json.loads(Path(path).read_text(encoding='utf-8'));m.validate_records(rows)
            if self.doc.records and not messagebox.askyesno('替换识别表','替换现有原文与译文？可撤销。',parent=self.win):return
            self.commit();self.doc.records=rows;self.refresh()
        except Exception as exc:self.error(str(exc))

    def export_rows(self):
        path=filedialog.asksaveasfilename(parent=self.win,defaultextension='.json',filetypes=[('JSON','*.json')])
        if not path:return
        try:
            self.editor.safe_destination(path)
            m.atomic_write(path,lambda p:Path(p).write_text(json.dumps(self.doc.records,ensure_ascii=False,indent=2),encoding='utf-8'))
        except Exception as exc:self.error(str(exc))

    def make_layers(self):
        if not self.guard():return
        if self.unsaved_row():return self.error('请先保存本行文本。')
        try:
            layers=imaging.layers_from_records(self.doc,self.rows(),auto_style=self.auto_style.get(),sensitivity=self.style_sensitivity.get())
            if not layers:raise ValueError('没有可生成的文字。')
            self.commit();self.doc.layers.extend(layers);self.editor.active=self.index;self.editor.selected=layers[-1].id
            self.view.set('合成');self.draw();self.note.set(f'已生成 {len(layers)} 个独立文字层。点击“返回排版”调整字体、粗细和四角，再迁移到 B。')
        except Exception as exc:self.error(str(exc))

    def generate_mask(self):
        if not self.guard():return
        try:
            mask=imaging.text_mask(self.doc.original,self.rows(),int(self.threshold.get()),int(self.dilation.get()),{'深色文字':'dark','浅色文字':'light','整框':'box'}[self.mask_mode.get()])
            self.commit();self.doc.mask=mask;self.view.set('蒙版');self.draw();self.note.set('蒙版已生成；请检查红色覆盖范围，补全漏笔画并擦除误选纹理。')
        except Exception as exc:self.error(str(exc))

    def clear_mask(self):
        if self.guard():self.commit();self.doc.mask=Image.new('L',self.doc.image.size);self.draw()

    def position(self,event):
        return (max(0,min(self.doc.image.width-1,round(self.canvas.canvasx(event.x)/self.scale))),max(0,min(self.doc.image.height-1,round(self.canvas.canvasy(event.y)/self.scale))))

    def press(self,event):
        if not self.guard():return
        self.start=self.position(event);self.last=self.start
        if self.mode.get() in ('蒙版画笔','蒙版橡皮','仿制画笔'):
            try:
                self.stroke_width=int(self.brush.get())
                if not 1<=self.stroke_width<=200:raise ValueError()
            except ValueError:self.start=None;return self.error('笔刷直径须为 1–200 像素。')
            if self.mode.get()=='仿制画笔':
                if self.clone_source is None:self.start=None;return self.error('请先 Alt + 单击设置仿制源点。')
                self.commit();self.clone_base=self.doc.image;self.view.set('修复底图');self.clone(self.start)
            else:self.stroke=self.doc.mask.copy();self.paint(self.start)
            self.draw()

    def sample(self,event):
        if not self.guard():return 'break'
        point=self.position(event);self.clone_source=point
        r,g,b,_=self.doc.image.getpixel(point);self.fill_color=f'#{r:02x}{g:02x}{b:02x}'
        self.note.set(f'已取色 {self.fill_color}；仿制源点 {point}。纯色填补对话框会使用该颜色。');return 'break'

    def clone(self,point):
        dx,dy=point[0]-self.start[0],point[1]-self.start[1]
        # Always sample pre-stroke pixels; source does not creep through prior clones.
        source=(self.clone_source[0]+dx,self.clone_source[1]+dy)
        candidate=imaging.clone_patch(self.clone_base,point,source,max(1,self.stroke_width//2))
        area=Image.new('L',candidate.size);x,y=point;r=self.stroke_width/2;ImageDraw.Draw(area).ellipse((x-r,y-r,x+r,y+r),fill=255)
        self.doc.image=imaging.apply_candidate(self.doc.image,candidate,area)

    def paint(self,point):
        p=ImageDraw.Draw(self.stroke);fill=0 if self.mode.get()=='蒙版橡皮' else 255;r=self.stroke_width/2
        p.line([self.last,point],fill=fill,width=self.stroke_width);x,y=point;p.ellipse((x-r,y-r,x+r,y+r),fill=fill);self.last=point

    def motion(self,event):
        if self.start is None:return
        point=self.position(event)
        if self.clone_base is not None:self.clone(point);self.draw()
        elif self.stroke is not None:self.paint(point);self.draw()
        else:
            self.canvas.delete('selection');a,b=self.start;c,d=point
            self.canvas.create_rectangle(a*self.scale,b*self.scale,c*self.scale,d*self.scale,outline='#ea783b',width=2,tags='selection')

    def release(self,event):
        if self.start is None:return
        point=self.position(event);a,b=self.start;c,d=point
        if self.clone_base is not None:self.clone_base=None
        elif self.stroke is not None:
            self.commit();self.doc.mask=self.stroke;self.stroke=None;self.view.set('蒙版')
        elif abs(a-c)>2 and abs(b-d)>2:
            box=(min(a,c),min(b,d),max(a,c),max(b,d))
            if self.mode.get()=='框选 OCR':self.roi=box
            else:
                mask=self.doc.mask.copy();ImageDraw.Draw(mask).rectangle(box,fill=255);self.commit();self.doc.mask=mask;self.view.set('蒙版')
        self.start=None;self.draw()

    def picture(self):
        mode=self.view.get()
        if mode=='原图':return self.doc.original
        if mode=='合成':return m.render(self.doc)
        if mode=='候选' and self.candidate is not None:return self.candidate
        if mode=='差异':return ImageEnhance.Brightness(ImageChops.difference(self.doc.original.convert('RGB'),self.doc.image.convert('RGB'))).enhance(4).convert('RGBA')
        if mode=='蒙版':
            image=self.doc.image.copy();overlay=Image.new('RGBA',image.size,'#ec4254');overlay.putalpha((self.stroke if self.stroke is not None else self.doc.mask).point(lambda v:round(v*.45)))
            return Image.alpha_composite(image,overlay)
        return self.doc.image

    def draw(self):
        if self.closed:return
        w,h=self.doc.image.size
        self.scale=max(.01,min((max(50,self.canvas.winfo_width())-4)/w,(max(50,self.canvas.winfo_height())-4)/h,1)) if self.zoom.get()=='适应' else int(self.zoom.get()[:-1])/100
        # Viewport-only raster: even 400% of a large image does not allocate a huge bitmap.
        image=self.picture();sw,sh=round(w*self.scale),round(h*self.scale)
        self.canvas.configure(scrollregion=(0,0,sw,sh))
        # Full preview is bounded; zoomed views use the visible crop with overlap.
        ox=max(0,int(self.canvas.canvasx(0)));oy=max(0,int(self.canvas.canvasy(0)))
        right=min(sw,ox+max(50,self.canvas.winfo_width())+4);bottom=min(sh,oy+max(50,self.canvas.winfo_height())+4)
        if right<=ox or bottom<=oy:ox=oy=0;right=min(sw,1000);bottom=min(sh,800)
        tile=image.transform((max(1,right-ox),max(1,bottom-oy)),Image.Transform.AFFINE,(1/self.scale,0,ox/self.scale,0,1/self.scale,oy/self.scale),Image.Resampling.BICUBIC)
        checker=Image.new('RGBA',tile.size,'white');p=ImageDraw.Draw(checker)
        for yy in range(0,tile.height,16):
            for xx in range(0,tile.width,16):
                if (xx//16+yy//16)%2:p.rectangle((xx,yy,xx+15,yy+15),fill='#dedede')
        checker.alpha_composite(tile);self.photo=ImageTk.PhotoImage(checker);self.canvas.delete('all');self.canvas.create_image(ox,oy,image=self.photo,anchor='nw')
        if self.view.get() in ('原图','蒙版'):
            for i,row in enumerate(self.doc.records,1):
                points=[(x*w*self.scale,y*h*self.scale) for x,y in row['box']];selected=row['id'] in self.table.selection()
                self.canvas.create_polygon(points,fill='',outline='#12775a' if selected else '#d18b35',width=2 if selected else 1)
                self.canvas.create_text(*points[0],text=str(i),anchor='sw',fill='#12775a')
        if self.roi:
            self.canvas.create_rectangle(*(v*self.scale for v in self.roi),outline='#337ec5',width=2,dash=(4,3))
        if self.view.get()=='修复底图':
            for row in self.residuals:self.canvas.create_polygon([(x*w*self.scale,y*h*self.scale) for x,y in row['box']],fill='',outline='#d63c35',width=2)

    def check_residuals(self):
        if not self.guard():return
        image=self.doc.image
        def done(rows):
            self.residuals=rows;self.view.set('修复底图');self.draw()
            self.note.set(f'修复底图检测到 {len(rows)} 段可能残字，已用红框标记。未检出不代表无残留；小标点、装饰字仍需放大检查。')
        self.run('本地残字复检',lambda:imaging.recognize(image),done)

    def local_repair(self,method):
        if not self.guard():return
        color='#ffffff'
        if method=='solid':
            color=colorchooser.askcolor(color=self.fill_color,parent=self.win)[1]
            if not color:return
        try:radius=int(self.radius.get())
        except ValueError:return self.error('修复半径须为整数。')
        image,mask=self.doc.image,self.doc.mask
        self.run('生成本地修复候选',lambda:imaging.repair(image,mask,method,radius,color),lambda image:self.show_candidate(image,True))

    def show_candidate(self,image,composited=False):
        self.candidate=image if composited else imaging.apply_candidate(self.doc.image,image,self.doc.mask);self.view.set('候选');self.draw()
        self.candidate_note.set('候选已就绪；请在左侧切换原图、候选并放大检查。采用前不会写入底图。')
        self.note.set('候选已生成，尚未采用。请检查背景纹理、边缘、残留笔画。')

    def accept(self):
        if not self.guard():return
        if self.candidate is None:return self.error('尚无有效候选；修改蒙版后需重新生成。')
        image=self.candidate;self.commit();self.doc.image=image;self.view.set('修复底图');self.draw();self.candidate_note.set('已采用，可撤销。');self.note.set('修复已采用；原图与透明度保留。')

    def reject(self):self.candidate=None;self.view.set('修复底图');self.draw();self.candidate_note.set('候选已丢弃。')

    def restore(self):
        if self.guard() and messagebox.askyesno('恢复底图','恢复最初载入的底图？保留 OCR、蒙版和文字层；可撤销。',parent=self.win):
            self.commit();self.doc.image=self.doc.original;self.draw()

    def import_candidate(self):
        if not self.guard():return
        path=filedialog.askopenfilename(parent=self.win,filetypes=[('PNG 图片','*.png')])
        if path:
            try:self.show_candidate(m.open_image(path))
            except Exception as exc:self.error(str(exc))

    def undo(self,redo=False):
        if self.guard():self.editor.undo(redo);self.candidate=None;self.refresh()

    def export_picture(self,picture,name):
        path=filedialog.asksaveasfilename(parent=self.win,initialfile=name,defaultextension='.png',filetypes=[('PNG','*.png')])
        if not path:return
        try:self.editor.safe_destination(path);m.atomic_write(path,lambda p:picture.save(p,format='PNG'));self.note.set('已导出原始尺寸图片；不含选框与编号。')
        except Exception as exc:self.error(str(exc))

    def export_view(self):self.export_picture(self.picture(),'AB'[self.index]+'-'+self.view.get()+'.png')
    def export_mask(self):self.export_picture(self.doc.mask,'AB'[self.index]+'-mask.png')

    def inspect(self):
        try:messagebox.showinfo('检查报告',imaging.inspection(self.doc),parent=self.win)
        except Exception as exc:self.error(str(exc))

    def settings(self):
        if not self.guard():return
        if self.ai_dialog is not None and self.ai_dialog.winfo_exists():
            self.editor.modals.schedule(True);return self.ai_dialog
        config=self.editor.ai_config
        dialog=tk.Toplevel(self.win);self.ai_dialog=dialog
        dialog.title('联网 AI 配置 · 仅本次运行有效');dialog.geometry('650x500');dialog.minsize(620,500)
        body=ttk.Frame(dialog,padding=18);body.pack(fill='both',expand=True);vars={}
        for key,title in [('base_url','基础地址（含 /v1）'),('key','自己的 API Key'),('text_model','翻译模型名称'),('image_model','图像编辑模型名称')]:
            ttk.Label(body,text=title).pack(anchor='w',pady=(6,1));var=tk.StringVar(value=getattr(config,key));vars[key]=var
            ttk.Entry(body,textvariable=var,show='●' if key=='key' else '').pack(fill='x')
        enabled=tk.BooleanVar(value=config.enabled);ttk.Checkbutton(body,text='启用联网 AI（每次请求仍需确认）',variable=enabled).pack(anchor='w',pady=10)
        ttk.Label(body,text='兼容协议：Chat Completions JSON 翻译；Images Edits multipart 修图。\n地址和模型由你核对。密钥只留在内存，不保存进项目或配置文件。\n不读取环境变量密钥，不自动重试、不切换服务、不跟随重定向。',wraplength=600).pack(anchor='w')
        def close():
            self.editor.modals.dismiss(dialog);self.ai_dialog=None;dialog.destroy()
        def save():
            candidate=ai_client.Config(**{k:v.get().strip() for k,v in vars.items()},enabled=enabled.get())
            self.editor.ai_config=candidate;close();self.note.set('AI 配置已保存在本次运行内存中；尚未发送请求。')
        ttk.Button(body,text='保存本次配置（不联网）',command=save).pack(fill='x',pady=12)
        ttk.Button(body,text='清除密钥',command=lambda:vars['key'].set('')).pack(fill='x');dialog.protocol('WM_DELETE_WINDOW',close)
        dialog.bind('<Escape>',lambda event:close());self.editor.modals.open(dialog)
        return dialog

    def consent(self,kind,detail):
        config=copy.copy(self.editor.ai_config)
        try:config.validate(kind)
        except Exception as exc:self.error(str(exc));return None
        endpoint=config.base_url.rstrip('/')+('/chat/completions' if kind=='text' else '/images/edits')
        model=config.text_model if kind=='text' else config.image_model
        if messagebox.askyesno('确认发送与可能计费',f'发送到：{endpoint}\n模型：{model}\n\n{detail}\n\n将使用你配置的密钥，服务商可能收费。确认发送？',parent=self.win):return config
        return None

    def translate(self):
        if not self.guard():return
        if self.unsaved_row():return self.error('请先保存本行文本。')
        rows=copy.deepcopy(self.rows());context=self.context.get();language=self.language.get()
        if not rows:return self.error('没有可翻译的行。')
        config=self.consent('text',f'发送选中的 {len(rows)} 行原文、目标语言和上下文；不发送图片。返回译文会填入校对表，可撤销。')
        if not config:return
        def done(output):
            self.commit()
            for row in self.doc.records:
                if row['id'] in output:row['target']=output[row['id']]
            self.refresh();self.note.set('译文已填入校对表；请逐行检查语义与称谓，尚未生成文字层。')
        self.run('AI 翻译',lambda:ai_client.translate(config,rows,context,language),done)

    def ai_repair(self):
        if not self.guard():return
        if not self.doc.mask.getbbox():return self.error('请先准备去字蒙版。')
        prompt=simpledialog.askstring('修复要求','输入发给图像服务的修复要求：',initialvalue='Remove text only within the transparent mask area and restore the underlying background texture. Preserve composition, lighting, edges and all unmasked content. Do not add text.',parent=self.win)
        if not prompt:return
        config=self.consent('image',f'发送整张当前底图（{self.doc.image.width} × {self.doc.image.height}）、去字蒙版和修复要求。即使只修局部，服务商也会收到整图。返回结果仅作为待确认候选。')
        if not config:return
        image,mask=self.doc.image,self.doc.mask
        self.run('AI 背景修复',lambda:ai_client.edit_background(config,image,mask,prompt),self.show_candidate)
