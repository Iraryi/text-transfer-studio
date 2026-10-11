"""Per-line, local font candidate matching. Estimates, never font identification claims."""
import copy
import math
from functools import lru_cache
from pathlib import Path
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
if __package__:from . import model as m
else:import model as m


def font_candidates():
    names=['msyh.ttc','msyhbd.ttc','msyhl.ttc','simsun.ttc','simhei.ttf','simkai.ttf','simfang.ttf',
           'YuGothR.ttc','YuGothB.ttc','msgothic.ttc','arial.ttf','arialbd.ttf','times.ttf','timesbd.ttf']
    paths=[str(Path('C:/Windows/Fonts')/name) for name in names if (Path('C:/Windows/Fonts')/name).is_file()]
    return paths or [m.default_font()]


@lru_cache(maxsize=64)
def characters(path):
    from fontTools.ttLib import TTFont
    with TTFont(path,fontNumber=0) as font:return frozenset((font.getBestCmap() or {}).keys())


@lru_cache(maxsize=512)
def template(text,path,stroke,size=64):
    layer=m.Layer(text=text,font=path,size=size,spacing=0,outline=stroke,color='#000000',outline_color='#000000')
    tile=m.text_tile(layer).getchannel('A');box=tile.getbbox()
    if not box:return None
    mask=np.asarray(tile.crop(box))>127;ys,xs=np.where(mask)
    if not len(xs):return None
    return mask[ys.min():ys.max()+1,xs.min():xs.max()+1]


def observed(image,row):
    points=np.float32([(x*image.width,y*image.height) for x,y in row['box']])
    w=(math.dist(points[0],points[1])+math.dist(points[2],points[3]))/2
    h=(math.dist(points[0],points[3])+math.dist(points[1],points[2]))/2
    scale=min(1,1200/max(w,1),256/max(h,1));width=max(2,round(w*scale));height=max(2,round(h*scale))
    matrix=cv2.getPerspectiveTransform(points,np.float32([[0,0],[width,0],[width,height],[0,height]]))
    rgba=cv2.warpPerspective(np.asarray(image),matrix,(width,height),borderMode=cv2.BORDER_REPLICATE)
    rgb=rgba[:,:,:3];gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    threshold,_=cv2.threshold(gray,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)
    border=np.concatenate([gray[0],gray[-1],gray[:,0],gray[:,-1]])
    dark=float(np.median(border))>=threshold
    ink=(gray<threshold if dark else gray>threshold)&(rgba[:,:,3]>0)
    # Otsu may return 0 for pure black/white: include threshold for dark glyphs.
    if dark:ink=(gray<=threshold)&(rgba[:,:,3]>0)
    ys,xs=np.where(ink)
    if len(xs)<8:raise ValueError('字形对比度不足，需人工设置。')
    left,right=int(xs.min()),int(xs.max())+1;top,bottom=int(ys.min()),int(ys.max())+1
    values=gray[ink];cut=np.quantile(values,.08 if dark else .92)
    core=ink & (gray<=cut if dark else gray>=cut)
    color=np.median(rgb[core],axis=0).round().astype(int)
    return ink[top:bottom,left:right],scale,'#'+''.join(f'{c:02x}' for c in color),float(np.mean(ink))


def similarity(a,b):
    shape=(min(640,max(64,a.shape[1])),min(128,max(32,a.shape[0])))
    aa=cv2.resize(a.astype('uint8'),shape,interpolation=cv2.INTER_NEAREST)>0
    bb=cv2.resize(b.astype('uint8'),shape,interpolation=cv2.INTER_NEAREST)>0
    union=np.logical_or(aa,bb).sum()
    overlap=float(np.logical_and(aa,bb).sum()/max(1,union))
    ratio=(a.shape[1]/a.shape[0])/(b.shape[1]/b.shape[0])
    return .8*overlap+.2*math.exp(-abs(math.log(ratio))*2)


def estimate_line(image,row,fonts=None):
    ink,scale,color,density=observed(image,row)
    text=row['source'].strip()
    if not text or len(text)>160 or '\n' in text:raise ValueError('长行或多行请拆分后估计。')
    codepoints={ord(c) for c in text if not c.isspace()}
    candidates=fonts or font_candidates();ranked=[]
    for path in candidates:
        try:
            if not codepoints<=characters(path):continue
            for stroke in (0,1,2):
                glyph=template(text,path,stroke)
                if glyph is not None:ranked.append((similarity(ink,glyph)-stroke*.002,path,stroke,glyph.shape[0]))
        except (OSError,ValueError):continue
    if not ranked:raise ValueError('候选字体不含原文字形；请手动选择字库。')
    ranked.sort(reverse=True,key=lambda x:x[0])
    # Refine at native pixel sizes: small hinted glyphs differ from a downscaled 64px glyph.
    refined=[]
    for _,path,stroke,gh in ranked[:12]:
        estimate=round(ink.shape[0]/scale/gh*64)
        for size in range(max(6,estimate-2),min(600,estimate+2)+1):
            thickness=max(0,min(30,round(stroke*size/64)))
            glyph=template(text,path,thickness,size)
            if glyph is not None:refined.append((similarity(ink,glyph)-thickness*.002,path,thickness,size))
    refined.sort(reverse=True,key=lambda x:x[0]);score,path,thickness,size=refined[0]
    points=row['box'];angle=-math.degrees(math.atan2((points[1][1]-points[0][1])*image.height,(points[1][0]-points[0][0])*image.width))
    return {'font':path,'size':size,'color':color,'outline':thickness,'outline_color':color,'angle':angle,
            'match_score':round(max(0,score),4),'ink_density':round(density,4),
            'font_margin':round(max(0,score-max((s for s,p,_,_ in refined if p!=path),default=0)),4),
            'alternatives':[{'font':p,'score':round(s,4)} for s,p,_,_ in ranked[:3]],
            'note':'候选字库近似匹配；低对比、纹理或 OCR 错字会降低可靠性。'}


def estimate_rows(image,rows,fonts=None):
    result=copy.deepcopy(rows)
    for row in result:
        try:row['style']=estimate_line(image,row,fonts)
        except (ValueError,OSError) as exc:row['style']={'note':str(exc),'match_score':0}
    return result


def accepted_style(style, enabled=False, sensitivity=0):
    """Conservative opt-in. Sensitivity is acceptance leniency, not a probability."""
    if not 0 <= sensitivity <= 100:
        raise ValueError('样式敏感度必须在 0–100 之间。')
    if not enabled:
        return {}
    threshold = .90 - .45*sensitivity/100
    margin = .04*(1-sensitivity/100)
    # Legacy estimates lack runner-up evidence: only opt in at highest sensitivity.
    if style.get('match_score', 0) < threshold or style.get('font_margin', 0) < margin:
        return {}
    return style
