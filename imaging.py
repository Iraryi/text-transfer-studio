"""Offline OCR, masks and local restoration. No image is edited in place."""
import uuid
import math
import threading
from pathlib import Path
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageChops
if __package__:
    from . import model as m, style_match
else:
    import model as m
    import style_match

_engine=None
_ocr_lock=threading.Lock()


def recognize(image, roi=None):
    # A cancelled UI wait may leave inference running; never share the engine concurrently.
    with _ocr_lock:return _recognize(image,roi)


def _recognize(image, roi=None):
    global _engine
    if _engine is None:
        import rapidocr
        directory=Path(rapidocr.__file__).parent/'models'
        paths={'Det':'PP-OCRv6_det_small.onnx','Rec':'PP-OCRv6_rec_small.onnx','Cls':'ch_ppocr_mobile_v2.0_cls_mobile.onnx'}
        params={'Global.log_level':'error','EngineConfig.onnxruntime.intra_op_num_threads':4,
                'EngineConfig.onnxruntime.inter_op_num_threads':1,'Global.text_score':.25}
        for key,name in paths.items():
            path=directory/name
            if not path.is_file():raise ValueError('缺少本地 OCR 模型，请重新解压完整安装包；不会自动联网下载。')
            params[key+'.model_path']=str(path)
        _engine=rapidocr.RapidOCR(params=params)
    left,top,right,bottom=roi or (0,0,*image.size)
    region=image.crop((left,top,right,bottom))
    white=Image.new('RGBA',region.size,'white');white.alpha_composite(region.convert('RGBA'))
    result=_engine(cv2.cvtColor(np.asarray(white.convert('RGB')),cv2.COLOR_RGB2BGR))
    rows=[]
    if result.txts is None:return rows
    for box,text,score in zip(result.boxes,result.txts,result.scores):
        points=[[max(0,min(1,(float(x)+left)/image.width)),max(0,min(1,(float(y)+top)/image.height))] for x,y in box]
        if not m.convex(points):continue
        rows.append({'id':uuid.uuid4().hex,'source':text,'recognized':text,'target':'','score':float(score),'box':points})
    m.validate_records(rows)
    return rows


def clone_patch(image,point,source,radius):
    if not 1<=radius<=100:raise ValueError('仿制半径须为 1–100。')
    x,y=point;sx,sy=source
    left=max(-radius,-x,-sx);top=max(-radius,-y,-sy)
    right=min(radius+1,image.width-x,image.width-sx);bottom=min(radius+1,image.height-y,image.height-sy)
    if right<=left or bottom<=top:return image
    patch=image.crop((sx+left,sy+top,sx+right,sy+bottom))
    mask=Image.new('L',patch.size);ImageDraw.Draw(mask).ellipse((-radius-left,-radius-top,radius-left,radius-top),fill=255)
    result=image.copy();result.paste(patch,(x+left,y+top),mask);result.putalpha(image.getchannel('A'))
    return result


def text_mask(image, records, threshold=150, dilation=2, mode='dark'):
    m.validate_records(records)
    if not 0<=threshold<=255 or not 0<=dilation<=30:raise ValueError('阈值须在 0–255，扩张在 0–30。')
    region=Image.new('L',image.size);pen=ImageDraw.Draw(region)
    for row in records:pen.polygon([(round(x*image.width),round(y*image.height)) for x,y in row['box']],fill=255)
    gray=np.asarray(image.convert('L'))
    ink=np.ones(gray.shape,dtype=bool) if mode=='box' else (gray<threshold if mode=='dark' else gray>threshold)
    mask=(ink & (np.asarray(region)>0)).astype('uint8')*255
    if dilation:mask=cv2.dilate(mask,np.ones((dilation*2+1,dilation*2+1),dtype='uint8'))
    mask[np.asarray(image.getchannel('A'))==0]=0
    return Image.fromarray(mask)


def repair(image,mask,method='telea',radius=3,color='#ffffff'):
    if mask.size!=image.size or not mask.getbbox():raise ValueError('请先绘制或生成非空去字蒙版。')
    if not 1<=radius<=30:raise ValueError('修复半径须为 1–30 像素。')
    if method=='solid':candidate=Image.new('RGBA',image.size,color)
    else:
        pixels=cv2.cvtColor(np.asarray(image.convert('RGB')),cv2.COLOR_RGB2BGR)
        restored=cv2.inpaint(pixels,np.asarray(mask.convert('L')),radius,cv2.INPAINT_NS if method=='ns' else cv2.INPAINT_TELEA)
        candidate=Image.fromarray(cv2.cvtColor(restored,cv2.COLOR_BGR2RGB)).convert('RGBA')
    return apply_candidate(image,candidate,mask)


def apply_candidate(image,candidate,mask):
    if candidate.size!=image.size or mask.size!=image.size:raise ValueError('候选图尺寸与原图不符；请先人工核对并调整尺寸，不会自动拉伸。')
    result=Image.composite(candidate.convert('RGBA'),image,mask.convert('L'))
    result.putalpha(image.getchannel('A'))
    return result


def layers_from_records(doc,records,use_target=True,perspective=False,auto_style=False,sensitivity=0):
    m.validate_records(records)
    layers=[]
    for row in records:
        text=(row['target'] or row['source']) if use_target else row['source']
        if not text.strip():continue
        points=[(x*doc.image.width,y*doc.image.height) for x,y in row['box']]
        width=math.dist(points[0],points[1]);height=math.dist(points[0],points[3])
        layer=m.Layer(text=text,size=max(6,min(600,round(height*.9))),spacing=0,
                      x=sum(p[0] for p in row['box'])/4,y=sum(p[1] for p in row['box'])/4,name='OCR 文字')
        style=style_match.accepted_style(row.get('style',{}),auto_style,sensitivity)
        for key in ('font','size','color','outline','outline_color','angle'):
            if key in style:setattr(layer,key,style[key])
        if not Path(layer.font).is_file():layer.font=m.default_font()
        # Preserve source style where possible, but do not silently create missing-glyph boxes.
        if not {ord(c) for c in text if not c.isspace()}<=style_match.characters(layer.font):
            bold=any(part in Path(layer.font).stem.lower() for part in ('bd','bold'))
            fallback=Path('C:/Windows/Fonts/msyhbd.ttc') if bold else Path(m.default_font())
            layer.font=str(fallback if fallback.is_file() else Path(m.default_font()))
        tile=m.text_tile(layer)
        if not perspective and tile.width>width:layer.size=max(6,round(layer.size*width/tile.width))
        if perspective:layer.quad=[list(p) for p in row['box']]
        layers.append(layer)
    if len(layers)+len(doc.layers)>200:raise ValueError('生成后超过 200 个文字层，请分批处理。')
    return layers


def inspection(doc):
    original=np.asarray(doc.original);current=np.asarray(doc.image)
    changed=np.any(original!=current,axis=2)
    report=[f'画布：{doc.image.width} × {doc.image.height}；文字层：{len(doc.layers)}；OCR 行：{len(doc.records)}',
            f'底图变动像素：{int(changed.sum())}（当前蒙版外 {int((changed & (np.asarray(doc.mask)==0)).sum())}，可含之前已接受的修复）',
            '底图透明度：'+('与原图完全一致' if np.array_equal(original[:,:,3],current[:,:,3]) else '与原图不同，请检查'),
            f'OCR 低置信度行（< 85%）：{sum(r["score"]<.85 for r in doc.records)}；未填译文：{sum(not r["target"] for r in doc.records)}']
    from fontTools.ttLib import TTFont
    for i,layer in enumerate(doc.layers,1):
        if not layer.visible:continue
        if any(x<0 or y<0 or x>doc.image.width or y>doc.image.height for x,y in m.corners(layer,doc.image.size)):
            report.append(f'文字层 {i} 有超出画布的部分。')
        with TTFont(layer.font,fontNumber=0) as font:
            cmap=font.getBestCmap() or {}
            missing=sorted({c for c in layer.text if not c.isspace() and ord(c) not in cmap})
        if missing:report.append(f'文字层 {i} 字体缺字：'+''.join(missing))
    report.append(f'逐行样式未估计或近似分数低（< 0.65）：{sum(r.get("style",{}).get("match_score",0)<.65 for r in doc.records)}')
    report.append('自动检查不能证明译义、阅读顺序、字体风格或修复纹理正确，请逐行校对并以 100% 比例检查。')
    return '\n'.join(report)
