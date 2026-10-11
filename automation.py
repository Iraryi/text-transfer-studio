"""Local, workspace-scoped automation. No API keys, HTTP or shell execution."""
import copy
from dataclasses import asdict
import math
from pathlib import Path
if __package__:
    from . import model as m, layout, imaging, style_match
else:
    import model as m
    import layout
    import imaging
    import style_match


class Session:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir() or self.workspace == Path(self.workspace.anchor):
            raise ValueError('请指定专用工作文件夹，不可使用磁盘根目录。')
        self.docs = [m.blank(), m.blank()]
        self.inputs = set()
        self.history = m.History()

    def path(self, value, output=False, overwrite=False):
        p = Path(value)
        if not p.is_absolute(): p = self.workspace / p
        p = p.resolve()
        if not p.is_relative_to(self.workspace) or p == self.workspace:
            raise ValueError('路径必须位于已授权的工作文件夹内。')
        if output:
            if p in self.inputs: raise ValueError('不能覆盖载入的原图或原项目，请另存。')
            if p.exists() and not overwrite: raise ValueError('文件已存在；请换名或显式指定 overwrite=true。')
            if not p.parent.is_dir(): raise ValueError('输出目录不存在。')
        elif not p.is_file(): raise ValueError('输入文件不存在。')
        return p

    def font(self, value):
        p = Path(value).resolve()
        system = Path('C:/Windows/Fonts').resolve()
        unix = Path('/usr/share/fonts').resolve()
        if not any(p.is_relative_to(root) for root in (self.workspace, system, unix)):
            raise ValueError('字体必须位于工作文件夹或系统字体目录。')
        if p.suffix.lower() not in ('.ttf','.otf','.ttc') or not p.is_file():
            raise ValueError('字体文件不存在或格式不支持。')
        return str(p)

    def doc(self, canvas='A'):
        if canvas not in ('A','B'): raise ValueError('canvas 必须是 A 或 B。')
        return self.docs['AB'.index(canvas)]

    def layer(self, doc, layer_id):
        found=next((l for l in doc.layers if l.id==layer_id),None)
        if found is None: raise ValueError('未找到图层。')
        return found

    def state(self):
        return {'documents':[{'canvas':'AB'[i], 'size':list(d.image.size), 'title':d.title,
                             'records':copy.deepcopy(d.records), 'layers':[asdict(l) for l in d.layers]}
                            for i,d in enumerate(self.docs)]}

    def execute(self, name, a):
        # JSON-schema validation occurs at the MCP boundary; validation here protects state too.
        if name == 'state': return self.state()
        if name == 'open_image':
            p=self.path(a['path']); image=m.open_image(p); doc=self.doc(a.get('canvas','A'))
            if (doc.layers or doc.records) and not a.get('replace',False):
                raise ValueError('画布已有内容；需 replace=true 才能替换。')
            new=m.Document(image,p.name,source_path=str(p))
            self.docs[self.docs.index(doc)]=new;self.inputs.add(p);self.history=m.History()
            return self.state()
        if name == 'open_project':
            p=self.path(a['path'])
            if any(d.layers or d.records or d.source_path for d in self.docs) and not a.get('replace',False):
                raise ValueError('会替换当前会话；需 replace=true。')
            docs,warnings=m.load_project(p)
            for doc in docs:
                for layer in doc.layers:layer.font=self.font(layer.font)
                for row in doc.records:
                    if 'font' in row.get('style',{}):row['style']['font']=self.font(row['style']['font'])
            self.docs=docs;self.inputs.add(p);self.history=m.History()
            return dict(self.state(),warnings=warnings)
        if name == 'save_project':
            p=self.path(a['path'],True,a.get('overwrite',False))
            if p.suffix.lower()!='.textshift':raise ValueError('请使用 .textshift 扩展名。')
            m.save_project(p,self.docs);return {'path':str(p)}
        if name == 'undo':
            return {'changed':self.history.move(self.docs,a.get('redo',False))}
        doc=self.doc(a.get('canvas','A'))
        if name == 'ocr':
            if doc.records and not a.get('replace',False):raise ValueError('已有校对记录；需 replace=true。')
            roi=a.get('roi')
            if roi and not (0<=roi[0]<roi[2]<=doc.image.width and 0<=roi[1]<roi[3]<=doc.image.height):
                raise ValueError('OCR 区域必须在图内，且 left<right、top<bottom。')
            rows=imaging.recognize(doc.original,roi)
            self.history.push(self.docs);doc.records=rows;return {'records':rows}
        if name == 'set_records':
            rows=copy.deepcopy(a['records']);m.validate_records(rows)
            for row in rows:
                if 'font' in row.get('style',{}):row['style']['font']=self.font(row['style']['font'])
            self.history.push(self.docs);doc.records=rows;return {'count':len(rows)}
        if name == 'estimate_styles':
            rows=style_match.estimate_rows(doc.original,doc.records)
            self.history.push(self.docs);doc.records=rows
            return {'records':rows,'note':'仅候选，不自动改变文字层。'}
        if name == 'make_layers':
            layers=imaging.layers_from_records(doc,doc.records,use_target=a.get('use_target',True),
                perspective=a.get('perspective',False),auto_style=a.get('auto_style',False),sensitivity=a.get('sensitivity',0))
            self.history.push(self.docs);doc.layers.extend(layers)
            return {'layers':[asdict(l) for l in layers]}
        if name in ('add_layer','update_layer'):
            values=copy.deepcopy(a['properties'])
            allowed=set(m.Layer.__dataclass_fields__)-{'id'}
            if set(values)-allowed:raise ValueError('未知图层属性。')
            if 'font' in values:values['font']=self.font(values['font'])
            old=self.layer(doc,a['layer_id']) if name=='update_layer' else None
            if old is None and len(doc.layers)>=200:raise ValueError('图层数量超过上限。')
            layer=copy.deepcopy(old) if old else m.Layer()
            for key,value in values.items():setattr(layer,key,value)
            layer.validate();m.text_tile(layer)
            self.history.push(self.docs)
            if old:doc.layers[doc.layers.index(old)]=layer
            else:doc.layers.append(layer)
            return asdict(layer)
        if name in ('move_layer','center_layer','delete_layer'):
            old=self.layer(doc,a['layer_id'])
            if name=='delete_layer':
                self.history.push(self.docs);doc.layers.remove(old);return {'deleted':old.id}
            if name=='center_layer':new=layout.centered(old,doc.image.size,a.get('axis','both'))
            else:
                new=layout.translated(old,doc.image.size,a.get('dx',0),a.get('dy',0))
                if a.get('snap',False):new,_=layout.snap(new,doc.image.size,doc.layers,a.get('tolerance',6))
            self.history.push(self.docs);doc.layers[doc.layers.index(old)]=new;return asdict(new)
        if name == 'transfer':
            target=self.doc('B' if a.get('canvas','A')=='A' else 'A')
            selected=[self.layer(doc,i) for i in a['layer_ids']] if a.get('layer_ids') else list(doc.layers)
            if len(target.layers)+len(selected)>200:raise ValueError('目标超过 200 层。')
            self.history.push(self.docs)
            result=[m.transfer(l,doc,target,a.get('proportional',True)) for l in selected]
            return {'layers':[asdict(l) for l in result]}
        if name == 'make_mask':
            mask=imaging.text_mask(doc.original,doc.records,a.get('threshold',150),a.get('dilation',2),a.get('mode','dark'))
            self.history.push(self.docs);doc.mask=mask;return {'bounds':mask.getbbox()}
        if name == 'repair_background':
            image=imaging.repair(doc.image,doc.mask,a.get('method','telea'),a.get('radius',3),a.get('color','#ffffff'))
            self.history.push(self.docs);doc.image=image;return {'note':'仅蒙版区域修复；请预览检查。'}
        if name == 'apply_background':
            p=self.path(a['path']);image=imaging.apply_candidate(doc.image,m.open_image(p),doc.mask)
            self.history.push(self.docs);doc.image=image;self.inputs.add(p);return {'note':'仅采用蒙版内区域，保留透明度。'}
        if name == 'inspect':
            return {'report':imaging.inspection(doc)}
        if name == 'export':
            p=self.path(a['path'],True,a.get('overwrite',False))
            if p.suffix.lower()!='.png':raise ValueError('仅支持 PNG 导出。')
            kind=a.get('kind','composite')
            if kind=='background':im=doc.image
            elif kind=='mask':im=doc.mask
            else:im=m.render(doc,transparent=kind=='text')
            m.atomic_write(p,lambda path:im.save(path,format='PNG'));return {'path':str(p),'size':list(im.size)}
        raise ValueError('未知操作。')
