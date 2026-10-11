"""字迁 / TextShift: local-first OCR, restoration and A/B text-layer editor."""
from pathlib import Path
import argparse
import copy
import ctypes
import json
import sys
import tkinter as tk
from tkinter import ttk, filedialog, colorchooser, messagebox
from PIL import Image, ImageDraw, ImageTk, ImageFont
if __package__:
    from . import model as m, ai_client, layout
    from .window_manager import ModalWindows
else:
    import model as m
    import ai_client
    import layout
    from window_manager import ModalWindows

BG = '#eef2ef'
INK = '#173b32'
ACCENT = '#16765d'


def font_label(path):
    known={'msyh.ttc':'微软雅黑 · 常规','msyhbd.ttc':'微软雅黑 · 粗体','msyhl.ttc':'微软雅黑 · 细体',
           'simsun.ttc':'宋体','simhei.ttf':'黑体','simkai.ttf':'楷体','simfang.ttf':'仿宋',
           'arial.ttf':'Arial · 常规','arialbd.ttf':'Arial · 粗体'}
    name=Path(path).name
    return known.get(name.lower(),name)


def contrast_color(color):
    rgb=[int(color[i:i+2],16) for i in (1,3,5)]
    return '#ffffff' if sum(a*b for a,b in zip(rgb,(.2126,.7152,.0722)))<140 else '#173b32'


class Editor:
    def __init__(self, root, demo=False):
        self.root = root
        # Match point fonts to the pixel-based layout; Windows display scaling
        # otherwise enlarges only fonts and clips the fixed-width inspector.
        self.root.tk.call('tk','scaling',96/72)
        self.docs = [m.blank(), m.blank()]
        self.active, self.selected = 0, None
        self.history = m.History()
        self.dirty = False
        self.project_path = None
        self.photo = [None,None]
        self.geometry = [None,None]
        self.drag = None
        self.snap_guides = []
        self.pending = None
        self.loading = False
        self.fonts = {font_label(m.default_font()):m.default_font()}
        self.fonts.update({font_label(p):str(p) for p in Path('C:/Windows/Fonts').glob('*') if p.suffix.lower() in ('.ttf','.otf','.ttc')})
        self.status = tk.StringVar(value='本地离线 · 原图不覆盖 · 在 A 中创建文字，再一键迁移到 B')
        self.mode = tk.StringVar(value='移动')
        self.proportional = tk.BooleanVar(value=True)
        self.transfer_scope = tk.StringVar(value='选中层')
        self.snap_enabled = tk.BooleanVar(value=False)
        self.auto_style = tk.BooleanVar(value=False)
        self.style_sensitivity = tk.DoubleVar(value=0)
        self.vars = {}
        self.ai_config = ai_client.Config()
        self.modals = ModalWindows(root)
        self.build_ui()
        if demo:
            self.demo()
        self.refresh()
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.root.bind('<Control-s>',lambda e:self.save())
        self.root.bind('<Control-o>',lambda e:self.open_project())
        self.root.bind('<Control-z>',self.undo_event)
        self.root.bind('<Control-y>',lambda e:self.undo_event(e,True))
        self.root.bind('<Delete>',self.delete_event)
        for key in ('Up','Down','Left','Right'):
            self.root.bind('<'+key+'>',self.nudge_event)

    def build_ui(self):
        root = self.root
        root.title('字迁 TextShift — 双画布文字工作台')
        root.geometry('1440x900');root.minsize(1100,740);root.configure(bg=BG)
        style=ttk.Style();style.theme_use('clam')
        style.configure('.',font=('Microsoft YaHei UI',10),background=BG,foreground=INK)
        style.configure('TButton',padding=(10,7),background='#ffffff',borderwidth=1)
        style.configure('Accent.TButton',background=ACCENT,foreground='white',padding=(16,10))
        style.map('Accent.TButton',background=[('active','#125844')])
        style.configure('TNotebook',background=BG)
        top=ttk.Frame(root,padding=(22,16));top.pack(fill='x')
        ttk.Label(top,text='字迁',font=('Microsoft YaHei UI',24,'bold')).pack(side='left')
        ttk.Label(top,text='TEXTSHIFT  /  双画布文字工作台',foreground='#678078').pack(side='left',padx=16)
        for title,command in [('打开项目',self.open_project),('保存项目',self.save),('撤销',self.undo),('重做',lambda:self.undo(True))]:
            ttk.Button(top,text=title,command=command).pack(side='right',padx=4)
        ttk.Button(top,text='连接外部 AI',command=self.mcp_settings).pack(side='right',padx=4)
        body=ttk.Frame(root,padding=(16,0,16,10));body.pack(fill='both',expand=True)
        inspector=ttk.Frame(body,width=310);inspector.pack(side='right',fill='y',padx=(14,0));inspector.pack_propagate(False)
        sidebar=tk.Canvas(inspector,bg=BG,highlightthickness=0,width=290)
        scroll=ttk.Scrollbar(inspector,orient='vertical',command=sidebar.yview)
        scroll.pack(side='right',fill='y');sidebar.pack(side='left',fill='both',expand=True)
        sidebar.configure(yscrollcommand=scroll.set)
        side=ttk.Frame(sidebar,padding=8);side_window=sidebar.create_window(0,0,window=side,anchor='nw')
        side.bind('<Configure>',lambda e:sidebar.configure(scrollregion=sidebar.bbox('all')))
        sidebar.bind('<Configure>',lambda e:sidebar.itemconfigure(side_window,width=e.width))
        def scroll_inspector(event):
            if str(event.widget).startswith(str(side)) and not isinstance(event.widget,(tk.Text,tk.Listbox,ttk.Combobox,ttk.Spinbox)):
                sidebar.yview_scroll(-int(event.delta/120),'units');return 'break'
        root.bind('<MouseWheel>',scroll_inspector,add='+')
        workspace=ttk.Frame(body);workspace.pack(side='left',fill='both',expand=True)
        header=ttk.Frame(workspace);header.pack(fill='x',pady=(0,12))
        ttk.Label(header,text='01  识别与修图    /    02  校对与排版    /    03  迁移并导出',foreground='#577368').pack(side='left')
        ttk.Button(header,text='A 图识别 / 修图',command=lambda:self.workflow(0)).pack(side='right',padx=3)
        ttk.Button(header,text='B 图识别 / 修图',command=lambda:self.workflow(1)).pack(side='right',padx=3)
        boards=ttk.Frame(workspace);boards.pack(fill='both',expand=True)
        boards.columnconfigure((0,1),weight=1,uniform='boards');boards.rowconfigure(0,weight=1)
        self.canvases=[];self.doc_labels=[]
        for i in range(2):
            frame=ttk.Frame(boards,padding=8);frame.grid(row=0,column=i,sticky='nsew',padx=3)
            row=ttk.Frame(frame);row.pack(fill='x',pady=(0,8))
            label=ttk.Label(row,text=f'{"AB"[i]} / 空白画布',font=('Microsoft YaHei UI',12,'bold'));label.pack(side='left')
            self.doc_labels.append(label)
            ttk.Button(row,text='载入图片',command=lambda n=i:self.load_image(n)).pack(side='right')
            canvas=tk.Canvas(frame,bg='#dfe6e1',highlightthickness=2,highlightbackground='#ccd7cf',cursor='fleur')
            canvas.pack(fill='both',expand=True);self.canvases.append(canvas)
            canvas.bind('<Configure>',lambda e:self.schedule_render())
            canvas.bind('<ButtonPress-1>',lambda e,n=i:self.press(n,e))
            canvas.bind('<B1-Motion>',lambda e,n=i:self.motion(n,e))
            canvas.bind('<ButtonRelease-1>',lambda e:self.release())
            footer=ttk.Frame(frame);footer.pack(fill='x',pady=(8,0))
            ttk.Button(footer,text='导出合成 PNG',command=lambda n=i:self.export(n)).pack(side='left')
            ttk.Button(footer,text='仅导出透明文字层',command=lambda n=i:self.export(n,True)).pack(side='right')
        controls=ttk.Frame(workspace,padding=(4,14));controls.pack(fill='x')
        ttk.Button(controls,text='＋ 添加文字',style='Accent.TButton',command=self.add).pack(side='left')
        ttk.Button(controls,text='A → B',style='Accent.TButton',command=lambda:self.transfer(0)).pack(side='left',padx=8)
        ttk.Button(controls,text='B → A',command=lambda:self.transfer(1)).pack(side='left')
        ttk.Combobox(controls,textvariable=self.transfer_scope,values=['选中层','全部文字层'],state='readonly',width=10).pack(side='left',padx=8)
        ttk.Checkbutton(controls,text='字号随目标图等比缩放',variable=self.proportional).pack(side='right')
        ttk.Label(workspace,text='提示：每行想用不同字体或字重，就拆成多个文字层。迁移是独立副本，不会联动改动原图。',foreground='#60796e').pack(anchor='w',padx=4)
        ttk.Label(side,text='文字层',font=('Microsoft YaHei UI',14,'bold')).pack(anchor='w')
        self.layer_list=tk.Listbox(side,height=5,exportselection=False,font=('Microsoft YaHei UI',10),borderwidth=0,highlightthickness=1,selectbackground=ACCENT)
        self.layer_list.pack(fill='x',pady=6);self.layer_list.bind('<<ListboxSelect>>',self.choose_layer)
        row=ttk.Frame(side);row.pack(fill='x')
        for title,cmd in [('复制',self.duplicate),('上移',lambda:self.reorder(1)),('下移',lambda:self.reorder(-1)),('删除',self.delete)]:
            ttk.Button(row,text=title,width=4,command=cmd).pack(side='left',padx=1)
        ttk.Label(side,text='内容（支持换行）').pack(anchor='w',pady=(12,4))
        self.text=tk.Text(side,height=4,wrap='word',font=('Microsoft YaHei UI',10),undo=True)
        self.text.pack(fill='x');self.text.bind('<<Modified>>',self.text_changed)
        ttk.Label(side,text='字体文件 / 字重').pack(anchor='w',pady=(10,4))
        row=ttk.Frame(side);row.pack(fill='x')
        self.font_name=tk.StringVar(value=font_label(m.default_font()))
        self.font_box=ttk.Combobox(row,textvariable=self.font_name,values=sorted(self.fonts),state='readonly',width=21)
        self.font_box.pack(side='left',fill='x',expand=True);self.font_box.bind('<<ComboboxSelected>>',lambda e:self.apply())
        ttk.Button(row,text='…',width=3,command=self.pick_font).pack(side='right')
        grid=ttk.Frame(side);grid.pack(fill='x',pady=8);grid.columnconfigure((0,1),weight=1)
        for i,(key,label,initial,low,high) in enumerate([('size','字号 / px',48,6,600),('spacing','行距 / px',12,0,200),('outline','描边 / px',0,0,30),('opacity','不透明度 / %',100,0,100),('angle','旋转 / °',0,-360,360)]):
            cell=ttk.Frame(grid);cell.grid(row=i//2,column=i%2,sticky='ew',padx=3,pady=3)
            ttk.Label(cell,text=label).pack(anchor='w')
            var=tk.StringVar(value=str(initial));self.vars[key]=var
            spin=ttk.Spinbox(cell,from_=low,to=high,textvariable=var,width=10,command=self.apply)
            spin.pack(fill='x');spin.bind('<Return>',lambda e:self.apply());spin.bind('<FocusOut>',lambda e:self.apply())
        cell=ttk.Frame(grid);cell.grid(row=2,column=1,sticky='ew',padx=3,pady=3)
        ttk.Label(cell,text='文字对齐').pack(anchor='w')
        self.align=tk.StringVar(value='居中')
        box=ttk.Combobox(cell,textvariable=self.align,values=['左对齐','居中','右对齐'],state='readonly',width=9)
        box.pack(fill='x');box.bind('<<ComboboxSelected>>',lambda e:self.apply())
        self.color_buttons={}
        row=ttk.Frame(side);row.pack(fill='x',pady=4)
        for key,title in [('color','文字颜色'),('outline_color','描边颜色')]:
            button=tk.Button(row,text=title,relief='flat',command=lambda k=key:self.pick_color(k),font=('Microsoft YaHei UI',10))
            button.pack(side='left',expand=True,fill='x',padx=3);self.color_buttons[key]=button
        ttk.Separator(side).pack(fill='x',pady=12)
        ttk.Label(side,text='画布操作').pack(anchor='w')
        row=ttk.Frame(side);row.pack(fill='x',pady=5)
        for title in ['移动','四角变形']:
            ttk.Radiobutton(row,text=title,value=title,variable=self.mode,command=self.schedule_render).pack(side='left',padx=4)
        ttk.Button(side,text='重置四角变形',command=self.reset_quad).pack(fill='x',pady=4)
        ttk.Checkbutton(side,text='吸附：平行边缘 / 中心轴 / 对称位置',variable=self.snap_enabled).pack(anchor='w')
        ttk.Label(side,text='方向键 1 px · Shift 10 px；微调不吸附\n拖动时按 Alt 可临时关闭吸附',foreground='#60796e').pack(anchor='w')
        self.visible=tk.BooleanVar(value=True)
        ttk.Checkbutton(side,text='显示当前文字层',variable=self.visible,command=self.apply).pack(anchor='w',pady=4)
        ttk.Button(side,text='文字内部居中（多行）',command=lambda:self.center('text')).pack(fill='x',pady=4)
        row=ttk.Frame(side);row.pack(fill='x')
        for title,axis in [('水平居中','x'),('垂直居中','y'),('画布居中','both')]:
            ttk.Button(row,text=title,command=lambda a=axis:self.center(a),width=7).pack(side='left',expand=True,fill='x')
        ttk.Label(side,text='拖动文字移动位置；四角模式拖动圆点。\n项目会保存两张底图及可编辑图层。\n字体不嵌入项目，跨机器需准备相同字体。',foreground='#60796e',wraplength=270).pack(anchor='w',pady=10)
        ttk.Label(root,textvariable=self.status,padding=(22,9),background='#dce8df',wraplength=1350).pack(fill='x')

    def layer(self):
        return next((x for x in self.docs[self.active].layers if x.id==self.selected),None)

    def mcp_settings(self):
        folder=filedialog.askdirectory(parent=self.root,title='选择允许外部 AI 访问的专用工作文件夹')
        if not folder:return
        if Path(folder).resolve()==Path(Path(folder).anchor):return self.error('请勿授权整个磁盘，选择专用工作文件夹。')
        if getattr(sys,'frozen',False):
            command=str(Path(sys.executable).with_name('TextShiftMCP.exe'));args=[]
            if not Path(command).is_file():return self.error('缺少 TextShiftMCP.exe，请解压完整发行包。')
        else:
            command=sys.executable
            args=['-m','textshift_studio.mcp_server'] if __package__ else [str(Path(__file__).with_name('mcp_server.py').resolve())]
        config={'mcpServers':{'textshift':{'command':command,'args':args+['--workspace',str(Path(folder).resolve())]}}}
        dialog=tk.Toplevel(self.root);dialog.title('连接外部 AI / 通用 MCP');dialog.geometry('700x440')
        ttk.Label(dialog,text='将以下配置合并到支持 MCP 的客户端配置，再在客户端启用。\n无需 API Key；只授权所选文件夹。独立会话，不操控当前窗口。',padding=14).pack(fill='x')
        box=tk.Text(dialog,wrap='word',height=12);box.pack(fill='both',expand=True,padx=14)
        payload=json.dumps(config,ensure_ascii=False,indent=2);box.insert('1.0',payload);box.configure(state='disabled')
        def copy_config():
            self.root.clipboard_clear();self.root.clipboard_append(payload)
            self.status.set('MCP 配置已复制；请在 AI 客户端合并配置并启用。')
        ttk.Button(dialog,text='复制 MCP 配置',command=copy_config).pack(pady=10)
        def close():
            self.modals.dismiss(dialog);dialog.destroy()
        dialog.protocol('WM_DELETE_WINDOW',close);dialog.bind('<Escape>',lambda e:close())
        self.modals.open(dialog)

    def workflow(self,index):
        if not self.apply():return self.error('请先修正尚未应用的文字样式。')
        if __package__:from .workflow import Workflow
        else:from workflow import Workflow
        self.workflow_window=Workflow(self,index)

    def changed(self):
        self.dirty=True
        self.root.title('字迁 TextShift — 双画布文字工作台 *')

    def refresh(self):
        layer=self.layer();self.loading=True
        self.layer_list.delete(0,'end')
        for i,item in enumerate(reversed(self.docs[self.active].layers)):
            self.layer_list.insert('end',('● ' if item.visible else '○ ')+(item.text.splitlines() or ['空文字层'])[0][:24])
            if layer and item.id==layer.id:self.layer_list.selection_set(i)
        self.text.delete('1.0','end')
        if layer:
            self.text.insert('1.0',layer.text)
            for key,var in self.vars.items():var.set(str(getattr(layer,key)))
            name=font_label(layer.font);self.fonts[name]=layer.font;self.font_name.set(name)
            self.font_box.configure(values=sorted(self.fonts))
            self.align.set({'left':'左对齐','center':'居中','right':'右对齐'}[layer.align]);self.visible.set(layer.visible)
            for key,button in self.color_buttons.items():button.configure(bg=getattr(layer,key),fg=contrast_color(getattr(layer,key)))
        self.text.edit_modified(False);self.loading=False
        self.schedule_render()

    def schedule_render(self):
        if self.pending:self.root.after_cancel(self.pending)
        self.pending=self.root.after(50,self.draw)

    def draw(self):
        self.pending=None
        for i,(canvas,doc) in enumerate(zip(self.canvases,self.docs)):
            w,h=max(1,canvas.winfo_width()),max(1,canvas.winfo_height())
            scale=min((w-28)/doc.image.width,(h-28)/doc.image.height,1)
            if scale<=0:continue
            try:preview=m.render(doc,max(1,round(doc.image.width*scale)))
            except Exception as exc:self.status.set('预览错误：'+str(exc));continue
            scale=preview.width/doc.image.width
            x,y=(w-preview.width)/2,(h-preview.height)/2;self.geometry[i]=(x,y,scale)
            # Checkerboard reveals alpha; never enters exported pixels.
            checker=Image.new('RGBA',preview.size,'#ffffff');pen=ImageDraw.Draw(checker)
            for yy in range(0,preview.height,16):
                for xx in range(0,preview.width,16):
                    if (xx//16+yy//16)%2:pen.rectangle((xx,yy,xx+15,yy+15),fill='#e0e4e0')
            checker.alpha_composite(preview);self.photo[i]=ImageTk.PhotoImage(checker)
            canvas.delete('all');canvas.create_image(x,y,image=self.photo[i],anchor='nw')
            canvas.configure(highlightbackground=ACCENT if self.active==i else '#ccd7cf')
            if self.active==i:
                for a,b in self.snap_guides:
                    canvas.create_line(x+a[0]*scale,y+a[1]*scale,x+b[0]*scale,y+b[1]*scale,fill='#cc628b',dash=(4,3))
            self.doc_labels[i].configure(text=f'{"AB"[i]} / {doc.image.width} × {doc.image.height}')
            if self.active==i and self.layer():
                try:points=[(x+a*scale,y+b*scale) for a,b in m.corners(self.layer(),doc.image.size)]
                except Exception:continue
                canvas.create_polygon(points,fill='',outline=ACCENT,width=2,dash=(5,3))
                if self.mode.get()=='四角变形':
                    for a,b in points:canvas.create_oval(a-6,b-6,a+6,b+6,fill='white',outline=ACCENT,width=2)

    def choose_layer(self,event=None):
        selection=self.layer_list.curselection()
        if selection:
            self.selected=list(reversed(self.docs[self.active].layers))[selection[0]].id
            self.refresh()

    def add(self):
        if len(self.docs[self.active].layers)>=200:return self.error('每张图最多 200 个文字层。')
        self.history.push(self.docs);layer=m.Layer();self.docs[self.active].layers.append(layer)
        self.selected=layer.id;self.changed();self.refresh();self.text.focus_set();self.text.tag_add('sel','1.0','end-1c')

    def apply(self):
        if self.loading or not self.layer():return True
        old=self.layer();new=copy.deepcopy(old)
        try:
            new.text=self.text.get('1.0','end-1c')
            for key,var in self.vars.items():setattr(new,key,float(var.get()) if key=='angle' else int(var.get()))
            new.align={'左对齐':'left','居中':'center','右对齐':'right'}[self.align.get()]
            new.font=self.fonts[self.font_name.get()];new.visible=self.visible.get()
            m.text_tile(new)
        except Exception as exc:self.status.set('尚未应用：'+str(exc));return False
        if new==old:return True
        self.history.push(self.docs);self.docs[self.active].layers[self.docs[self.active].layers.index(old)]=new
        self.changed();self.schedule_render()
        self.status.set('样式已更新 · 导出按原图尺寸，不带选框')
        # Do not replace text widget during typing (caret/selection must survive).
        index=list(reversed(self.docs[self.active].layers)).index(new)
        self.layer_list.delete(index);self.layer_list.insert(index,('● ' if new.visible else '○ ')+(new.text.splitlines() or ['空文字层'])[0][:24]);self.layer_list.selection_set(index)
        return True

    def text_changed(self,event=None):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            if not self.loading:self.apply()

    def pick_font(self):
        path=filedialog.askopenfilename(filetypes=[('字体文件','*.ttf *.otf *.ttc')])
        if path:
            try:ImageFont.truetype(path,24)
            except Exception as exc:return self.error(str(exc))
            name=font_label(path);self.fonts[name]=path;self.font_box.configure(values=sorted(self.fonts));self.font_name.set(name);self.apply()

    def pick_color(self,key):
        layer=self.layer()
        if not layer:return
        color=colorchooser.askcolor(getattr(layer,key),parent=self.root)[1]
        if color:
            self.history.push(self.docs);setattr(layer,key,color);self.changed();self.refresh()

    def transfer(self,source):
        all_layers=self.transfer_scope.get()=='全部文字层'
        if not all_layers and (self.active!=source or not self.layer()):
            self.status.set(f'请先在 {"AB"[source]} 图中选中需要迁移的文字层。');return
        target=1-source
        if not self.apply():return self.error('请先修正右侧尚未应用的文字或样式。')
        items=list(self.docs[source].layers) if all_layers else [self.layer()]
        if not items:self.status.set('源画布还没有文字层。');return
        if len(self.docs[target].layers)+len(items)>200:return self.error('目标图层将超过 200 层上限。')
        self.history.push(self.docs)
        for original in items:item=m.transfer(original,self.docs[source],self.docs[target],self.proportional.get())
        self.active=target;self.selected=item.id;self.changed();self.refresh()
        self.status.set(f'已复制 {len(items)} 个文字层到 {"AB"[target]}，保留相对位置和文字样式；可继续拖动、变形。')

    def duplicate(self):
        layer=self.layer()
        if not layer:return
        if len(self.docs[self.active].layers)>=200:return self.error('图层数量已达上限。')
        self.history.push(self.docs);item=m.transfer(layer,self.docs[self.active],self.docs[self.active],False)
        item.x+=.02;item.y+=.02
        if item.quad:item.quad=[(x+.02,y+.02) for x,y in item.quad]
        self.selected=item.id;self.changed();self.refresh()

    def delete(self):
        layer=self.layer()
        if layer:self.history.push(self.docs);self.docs[self.active].layers.remove(layer);self.selected=None;self.changed();self.refresh()

    def reorder(self,delta):
        layer=self.layer()
        if not layer:return
        layers=self.docs[self.active].layers;i=layers.index(layer);j=i+delta
        if 0<=j<len(layers):self.history.push(self.docs);layers[i],layers[j]=layers[j],layers[i];self.changed();self.refresh()

    def center(self,axis='both'):
        if not self.apply():return
        layer=self.layer()
        if not layer:return
        doc=self.docs[self.active]
        try:candidate=layout.centered(layer,doc.image.size,axis)
        except ValueError as exc:return self.error(str(exc))
        if candidate==layer:return
        self.history.push(self.docs);doc.layers[doc.layers.index(layer)]=candidate
        self.snap_guides=[];self.changed();self.refresh()

    def nudge_event(self,event):
        # Never steal caret movement or spinbox/list navigation from input widgets.
        if event.widget not in self.canvases or self.drag:return
        if not self.apply() or not self.layer():return 'break'
        dx,dy={'Left':(-1,0),'Right':(1,0),'Up':(0,-1),'Down':(0,1)}[event.keysym]
        step=10 if event.state & 1 else 1
        doc=self.docs[self.active];old=self.layer()
        try:new=layout.translated(old,doc.image.size,dx*step,dy*step)
        except ValueError as exc:self.status.set(str(exc));return 'break'
        self.history.push(self.docs);doc.layers[doc.layers.index(old)]=new
        self.snap_guides=[];self.changed();self.schedule_render()
        self.status.set(f'已微调 {step} 个原图像素（不吸附） · Ctrl+Z 撤销')
        return 'break'

    def reset_quad(self):
        if self.layer():self.history.push(self.docs);self.layer().quad=None;self.changed();self.refresh()

    def press(self,index,event):
        if not self.apply():return
        self.snap_guides=[]
        self.canvases[index].focus_set()
        if self.geometry[index] is None:return
        x,y,s=self.geometry[index];px,py=(event.x-x)/s,(event.y-y)/s
        was_active=self.active==index;self.active=index;doc=self.docs[index]
        layer=self.layer() if was_active else None;handle=None
        if layer and self.mode.get()=='四角变形':
            for j,(a,b) in enumerate(m.corners(layer,doc.image.size)):
                if math_distance(px,py,a,b)*s<12:handle=j;break
        if handle is None:
            layer=None
            for candidate in reversed(doc.layers):
                if not candidate.visible:continue
                points=m.corners(candidate,doc.image.size)
                if point_inside((px,py),points):layer=candidate;break
        self.selected=layer.id if layer else None;self.refresh()
        if layer:
            self.drag={'index':index,'start':(px,py),'layer':copy.deepcopy(layer),'handle':handle,'saved':False}

    def motion(self,index,event):
        if not self.drag or index!=self.drag['index']:return
        doc=self.docs[index];layer=self.layer();original=self.drag['layer']
        x,y,s=self.geometry[index];px,py=(event.x-x)/s,(event.y-y)/s
        dx,dy=(px-self.drag['start'][0])/doc.image.width,(py-self.drag['start'][1])/doc.image.height
        if abs(dx)+abs(dy)<1e-5:return
        candidate=copy.deepcopy(original)
        if self.drag['handle'] is not None:
            candidate.quad=[(a/doc.image.width,b/doc.image.height) for a,b in m.corners(original,doc.image.size)]
            candidate.quad[self.drag['handle']]=(px/doc.image.width,py/doc.image.height)
        else:
            candidate.x+=dx;candidate.y+=dy
            if candidate.quad:candidate.quad=[(a+dx,b+dy) for a,b in candidate.quad]
        try:
            self.snap_guides=[]
            if self.drag['handle'] is None and self.snap_enabled.get() and not getattr(event,'state',0)&0x20000:
                candidate,self.snap_guides=layout.snap(candidate,doc.image.size,doc.layers,tolerance=6/s)
            candidate.validate()
        except ValueError:return
        if not self.drag['saved']:self.history.push(self.docs);self.drag['saved']=True
        doc.layers[doc.layers.index(layer)]=candidate;self.changed();self.schedule_render()

    def release(self):
        self.drag=None
        self.snap_guides=[];self.schedule_render()

    def undo(self,redo=False):
        if self.history.move(self.docs,redo):self.changed();self.refresh();self.status.set('已重做' if redo else '已撤销')

    def undo_event(self,event,redo=False):
        if isinstance(event.widget,(tk.Text,tk.Entry,ttk.Entry,ttk.Spinbox)):return
        self.undo(redo);return 'break'

    def delete_event(self,event):
        if event.widget in self.canvases or event.widget==self.layer_list:self.delete();return 'break'

    def load_image(self,index):
        path=filedialog.askopenfilename(filetypes=[('图片','*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff'),('所有文件','*.*')])
        if not path:return
        try:image=m.open_image(path)
        except Exception as exc:return self.error(str(exc))
        if self.docs[index].layers and not messagebox.askyesno('替换底图','替换底图并保留文字层？相对位置会保持，字号不自动改变。'):return
        self.docs[index].image=image;self.docs[index].title=Path(path).name;self.docs[index].source_path=str(Path(path).resolve())
        self.docs[index].original=image;self.docs[index].mask=Image.new('L',image.size);self.docs[index].records=[]
        self.active=index;self.selected=None;self.history=m.History();self.changed();self.refresh();self.status.set('底图已载入，原文件未改动。替换底图后撤销记录已清空。')

    def safe_destination(self,path):
        resolved=Path(path).resolve()
        if any(doc.source_path and resolved==Path(doc.source_path).resolve() for doc in self.docs):
            raise ValueError('不能覆盖正在编辑的原图，请使用新的文件名。')
        if self.project_path and resolved==Path(self.project_path).resolve():
            raise ValueError('不能用图片覆盖项目文件。')

    def export(self,index,transparent=False):
        if not self.apply():return self.error('请先修正右侧尚未应用的文字或样式。')
        path=filedialog.asksaveasfilename(defaultextension='.png',initialfile=f'{"AB"[index]}-'+('text' if transparent else 'composite')+'.png',filetypes=[('PNG 图片','*.png')])
        if not path:return
        try:
            self.safe_destination(path);image=m.render(self.docs[index],transparent=transparent)
            m.atomic_write(path,lambda temporary:image.save(temporary,format='PNG'))
            self.status.set('已导出：'+path)
        except Exception as exc:self.error(str(exc))

    def save(self):
        if not self.apply():return self.error('请先修正右侧尚未应用的文字或样式。')
        path=filedialog.asksaveasfilename(defaultextension='.textshift',initialfile=Path(self.project_path).name if self.project_path else '我的排字项目.textshift',filetypes=[('字迁项目','*.textshift')])
        if not path:return
        try:
            if any(doc.source_path and Path(path).resolve()==Path(doc.source_path).resolve() for doc in self.docs):raise ValueError('不能覆盖原始图片。')
            m.save_project(path,self.docs);self.project_path=path;self.dirty=False
            self.root.title('字迁 TextShift — '+Path(path).name);self.status.set('项目已保存，含原图、修复底图、蒙版、校对表和文字层；不含字体与 AI 密钥。')
        except Exception as exc:self.error(str(exc))

    def open_project(self):
        if self.dirty and not messagebox.askyesno('未保存修改','放弃当前未保存的修改并打开项目？'):return
        path=filedialog.askopenfilename(filetypes=[('字迁项目','*.textshift')])
        if not path:return
        try:documents,warnings=m.load_project(path)
        except Exception as exc:return self.error(str(exc))
        self.docs=documents;self.project_path=path;self.active=0;self.selected=None;self.history=m.History();self.dirty=False;self.refresh()
        self.root.title('字迁 TextShift — '+Path(path).name)
        self.status.set('项目已打开。' if not warnings else '缺少字体，已临时替换为默认字体：'+', '.join(warnings))
        if warnings:messagebox.showwarning('字体缺失','以下字体未找到，显示已临时替换。请重新选择原字体再导出：\n'+'\n'.join(warnings))

    def close(self):
        if self.dirty and not messagebox.askyesno('未保存修改','还有未保存的修改，确定退出？'):return
        if self.pending:self.root.after_cancel(self.pending)
        self.modals.shutdown()
        self.root.destroy()

    def error(self,message):
        self.status.set(message);messagebox.showerror('操作未完成',message,parent=self.root)

    def demo(self):
        # Neutral built-in example, code-drawn flat surfaces, not user game material.
        a=Image.new('RGBA',(1280,900),'#ebeadf');b=Image.new('RGBA',(1280,900),'#153f34')
        pen=ImageDraw.Draw(a);pen.rounded_rectangle((150,95,1130,805),radius=24,fill='#fbfaf4',outline='#c9cfbd',width=3)
        pen=ImageDraw.Draw(b);pen.rounded_rectangle((145,100,1135,800),radius=24,fill='#244e40',outline='#81956b',width=3)
        self.docs=[m.Document(a,'示例 A'),m.Document(b,'示例 B')]
        title=m.Layer(text='把文字，放到合适的位置。',size=62,x=.5,y=.4)
        subtitle=m.Layer(text='城市植物观察日记\n文字 · 字体 · 颜色 · 自由排版',size=31,color='#6f8268',spacing=22,x=.5,y=.58)
        self.docs[0].layers=[title,subtitle]
        copied=m.transfer(title,self.docs[0],self.docs[1]);copied.color='#eef0c5'
        small=m.transfer(subtitle,self.docs[0],self.docs[1]);small.color='#bbcfa5'
        self.active=0;self.selected=title.id


def math_distance(a,b,c,d):return ((a-c)**2+(b-d)**2)**.5


def point_inside(point,polygon):
    x,y=point
    signs=[(b[0]-a[0])*(y-a[1])-(b[1]-a[1])*(x-a[0]) for a,b in zip(polygon,polygon[1:]+polygon[:1])]
    return all(v>=0 for v in signs) or all(v<=0 for v in signs)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--demo',action='store_true');parser.add_argument('--ocr-demo',action='store_true');parser.add_argument('--self-test-dir');args=parser.parse_args()
    try:ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError,OSError):pass
    root=tk.Tk();editor=Editor(root,args.demo)
    if args.ocr_demo or args.self_test_dir:
        image=Image.new('RGBA',(1000,320),'white');pen=ImageDraw.Draw(image)
        for y,text,font_path,size,color in [(20,'城市植物观察日记','C:/Windows/Fonts/msyhbd.ttc',56,'#96352f'),(120,'今日は図書館で本を読みます。',m.default_font(),42,'#354e85'),(225,'TEXT SHIFT 2026','C:/Windows/Fonts/arial.ttf',32,'#316b4f')]:
            font=ImageFont.truetype(font_path if Path(font_path).is_file() else m.default_font(),size);pen.text((35,y),text,font=font,fill=color)
        editor.docs[0]=m.Document(image,'OCR 测试卡');editor.refresh();editor.workflow(0)
    if args.self_test_dir:
        if __package__:from .smoke_test import start
        else:from smoke_test import start
        start(editor,Path(args.self_test_dir))
    root.mainloop()


if __name__=='__main__':main()
