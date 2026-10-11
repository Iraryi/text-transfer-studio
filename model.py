"""Non-destructive text layers, perspective rendering and portable projects."""
from dataclasses import dataclass, field, asdict
from pathlib import Path
import copy
import io
import json
import math
import os
import tempfile
import uuid
import zipfile
from PIL import Image, ImageDraw, ImageFont, ImageOps

MAX_PIXELS = 40_000_000
MAX_TEXT = 4000


def default_font():
    for path in ('C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/simhei.ttf',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        if Path(path).is_file():
            return path
    raise ValueError('未找到默认字体，请选择 TTF、OTF 或 TTC 字体文件。')


@dataclass
class Layer:
    text: str = '在这里输入文字'
    font: str = field(default_factory=default_font)
    size: int = 48
    color: str = '#17312b'
    outline: int = 0
    outline_color: str = '#ffffff'
    opacity: int = 100
    spacing: int = 12
    align: str = 'center'
    angle: float = 0
    x: float = .5
    y: float = .5
    # Optional four document-normalized corners: TL, TR, BR, BL.
    quad: list | None = None
    visible: bool = True
    name: str = '文字层'
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def validate(self):
        if not isinstance(self.text, str) or len(self.text) > MAX_TEXT:
            raise ValueError('单层文字最多 4000 字。')
        if not 6 <= self.size <= 600 or not 0 <= self.outline <= 30:
            raise ValueError('字号或描边超出支持范围。')
        if not 0 <= self.opacity <= 100 or not 0 <= self.spacing <= 200:
            raise ValueError('透明度或行距无效。')
        if self.align not in ('left', 'center', 'right'):
            raise ValueError('无效的对齐方式。')
        if not all(math.isfinite(v) for v in (self.x, self.y, self.angle)):
            raise ValueError('无效的位置或角度。')
        if not -4 <= self.x <= 5 or not -4 <= self.y <= 5 or abs(self.angle) > 360:
            raise ValueError('位置或角度超出支持范围。')
        for color in (self.color, self.outline_color):
            if not isinstance(color, str) or len(color) != 7 or color[0] != '#':
                raise ValueError('颜色必须是 #RRGGBB。')
            int(color[1:], 16)
        if self.quad is not None:
            if len(self.quad) != 4 or any(len(p) != 2 for p in self.quad):
                raise ValueError('文字四角数据无效。')
            if any(not math.isfinite(v) or not -4 <= v <= 5 for p in self.quad for v in p):
                raise ValueError('文字四角超出支持范围。')
            if not convex(self.quad):
                raise ValueError('四角不能交叉或折叠。')


@dataclass
class Document:
    image: Image.Image
    title: str = '空白画布'
    layers: list = field(default_factory=list)
    source_path: str | None = None
    original: Image.Image | None = None
    mask: Image.Image | None = None
    records: list = field(default_factory=list)

    def __post_init__(self):
        # Images are copy-on-write throughout the editor; snapshots share unchanged pixels.
        if self.original is None:self.original = self.image
        if self.mask is None:self.mask = Image.new('L', self.image.size)


def blank():
    return Document(Image.new('RGBA', (1280, 720), '#f3f0e9'))


def open_image(source):
    with Image.open(source) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ValueError('图片不能超过 4000 万像素。')
        return ImageOps.exif_transpose(image).convert('RGBA')


def convex(points):
    signs = []
    for i in range(4):
        a, b, c = points[i], points[(i+1) % 4], points[(i+2) % 4]
        signs.append((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))
    return all(v > 1e-7 for v in signs)


def text_tile(layer, scale=1):
    layer.validate()
    font = ImageFont.truetype(layer.font, max(1, round(layer.size*scale)))
    stroke = round(layer.outline*scale)
    spacing = round(layer.spacing*scale)
    text = layer.text or ' '
    probe = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
    bounds = probe.multiline_textbbox((0, 0), text, font=font, spacing=spacing,
                                     align=layer.align, stroke_width=stroke)
    padding = max(2, stroke + 2)
    w, h = math.ceil(bounds[2]-bounds[0]+padding*2), math.ceil(bounds[3]-bounds[1]+padding*2)
    if w*h > 16_000_000 or max(w, h) > 12000:
        raise ValueError('该文字层过大，请降低字号或分成多个图层。')
    tile = Image.new('RGBA', (max(1, w), max(1, h)))
    draw = ImageDraw.Draw(tile)
    draw.multiline_text((padding-bounds[0], padding-bounds[1]), text,
                        font=font, spacing=spacing, align=layer.align,
                        fill=layer.color, stroke_width=stroke, stroke_fill=layer.outline_color)
    if layer.opacity != 100:
        tile.putalpha(tile.getchannel('A').point(lambda x: round(x*layer.opacity/100)))
    if layer.angle:
        tile = tile.rotate(layer.angle, Image.Resampling.BICUBIC, expand=True)
    return tile


def corners(layer, size, tile=None):
    w, h = size
    if layer.quad is not None:
        return [(x*w, y*h) for x, y in layer.quad]
    tile = tile or text_tile(layer)
    x, y = layer.x*w-tile.width/2, layer.y*h-tile.height/2
    return [(x,y),(x+tile.width,y),(x+tile.width,y+tile.height),(x,y+tile.height)]


def coefficients(destination, source):
    # Inverse homography, solved by pivoted Gaussian elimination (no OpenCV dependency).
    matrix = []
    for (x,y),(u,v) in zip(destination, source):
        matrix.append([x,y,1,0,0,0,-u*x,-u*y,u])
        matrix.append([0,0,0,x,y,1,-v*x,-v*y,v])
    for i in range(8):
        pivot = max(range(i,8), key=lambda j: abs(matrix[j][i]))
        matrix[i], matrix[pivot] = matrix[pivot], matrix[i]
        if abs(matrix[i][i]) < 1e-10:
            raise ValueError('四角过于接近，无法变形。')
        d = matrix[i][i]
        matrix[i] = [v/d for v in matrix[i]]
        for j in range(8):
            if j != i:
                d = matrix[j][i]
                matrix[j] = [a-d*b for a,b in zip(matrix[j],matrix[i])]
    return [matrix[i][8] for i in range(8)]


def render(doc, preview_width=None, transparent=False):
    scale = min(1, preview_width/doc.image.width) if preview_width else 1
    size = (max(1, round(doc.image.width*scale)), max(1, round(doc.image.height*scale)))
    result = Image.new('RGBA', size) if transparent else doc.image.resize(size, Image.Resampling.LANCZOS)
    for layer in doc.layers:
        if not layer.visible:
            continue
        # Keep layout measured in original pixels; downsample rendered text only for preview.
        tile = text_tile(layer)
        if layer.quad is None:
            pos = (round(layer.x*size[0]-tile.width*scale/2), round(layer.y*size[1]-tile.height*scale/2))
            if scale != 1:
                tile = tile.resize((max(1,round(tile.width*scale)),max(1,round(tile.height*scale))), Image.Resampling.LANCZOS)
            result.alpha_composite(tile, pos)
        else:
            destination = corners(layer, size)
            coeff = coefficients(destination, [(0,0),(tile.width,0),(tile.width,tile.height),(0,tile.height)])
            projected = tile.transform(size, Image.Transform.PERSPECTIVE, coeff, Image.Resampling.BICUBIC)
            result = Image.alpha_composite(result, projected)
    return result


def transfer(layer, source, target, proportional=True):
    item = copy.deepcopy(layer)
    item.id = uuid.uuid4().hex
    if proportional:
        ratio = min(target.image.width/source.image.width, target.image.height/source.image.height)
        item.size = max(6, min(600, round(item.size*ratio)))
        item.spacing = max(0,min(200,round(item.spacing*ratio)))
        item.outline = max(0,min(30,round(item.outline*ratio)))
    # Normalized centers and perspective corners map to the target dimensions.
    target.layers.append(item)
    return item


def atomic_write(path, writer):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.textshift-', suffix='.tmp', dir=path.parent)
    os.close(fd)
    try:
        writer(temporary)
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def save_project(path, documents):
    def write(temporary):
        manifest = {'format':'textshift','version':2,'documents':[]}
        with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
            for i, doc in enumerate(documents):
                name = f'image-{i}.png'
                content = io.BytesIO();doc.image.save(content,format='PNG')
                archive.writestr(name,content.getvalue())
                for kind,picture in [('original',doc.original),('mask',doc.mask)]:
                    content=io.BytesIO();picture.save(content,format='PNG')
                    archive.writestr(f'{kind}-{i}.png',content.getvalue())
                validate_records(doc.records)
                manifest['documents'].append({'image':name,'title':doc.title,'records':doc.records,'layers':[asdict(x) for x in doc.layers]})
            archive.writestr('project.json',json.dumps(manifest,ensure_ascii=False))
    atomic_write(path,write)


def load_project(path):
    documents, warnings = [], []
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) not in (3,7) or sum(x.file_size for x in archive.infolist()) > 600_000_000:
            raise ValueError('项目体积或条目数量异常。')
        if archive.getinfo('project.json').file_size > 8_000_000:
            raise ValueError('项目描述过大。')
        value = json.loads(archive.read('project.json'))
        if value.get('format') != 'textshift' or value.get('version') not in (1,2) or len(value['documents']) != 2:
            raise ValueError('不支持的项目格式。')
        expected={'project.json','image-0.png','image-1.png'}
        if value['version']==2:expected.update({'original-0.png','original-1.png','mask-0.png','mask-1.png'})
        if set(archive.namelist())!=expected or len(archive.namelist())!=len(expected):raise ValueError('项目含未知或重复文件。')
        seen = set()
        for i, item in enumerate(value['documents']):
            if item['image'] != f'image-{i}.png' or len(item['layers']) > 200:
                raise ValueError('项目图片或图层数量无效。')
            doc = Document(open_image(io.BytesIO(archive.read(item['image']))), str(item['title'])[:200])
            if value['version']==2:
                doc.original=open_image(io.BytesIO(archive.read(f'original-{i}.png')))
                doc.mask=open_image(io.BytesIO(archive.read(f'mask-{i}.png'))).convert('L')
                if doc.original.size!=doc.image.size or doc.mask.size!=doc.image.size:raise ValueError('原图或蒙版尺寸不匹配。')
                doc.records=item.get('records',[]);validate_records(doc.records)
            for data in item['layers']:
                layer = Layer(**data);layer.validate()
                if layer.id in seen:
                    raise ValueError('项目含重复图层编号。')
                seen.add(layer.id)
                if not Path(layer.font).is_file():
                    warnings.append(Path(layer.font).name)
                    layer.font = default_font()
                text_tile(layer)  # Validate font and allocation before adopting project.
                doc.layers.append(layer)
            documents.append(doc)
    return documents, warnings


class History:
    def __init__(self):
        self.undo_stack, self.redo_stack = [], []

    @staticmethod
    def snapshot(documents):
        return [(copy.deepcopy(doc.layers),doc.image,doc.original,doc.mask,copy.deepcopy(doc.records)) for doc in documents]

    def push(self, documents):
        self.undo_stack.append(self.snapshot(documents))
        self.undo_stack = self.undo_stack[-50:]
        # Limit retained raster history to approximately 256 MiB (shared images counted once).
        while len(self.undo_stack)>1:
            images={id(im):im for state in self.undo_stack for entry in state for im in entry[1:4]}
            if sum(im.width*im.height*len(im.getbands()) for im in images.values())<=256*1024**2:break
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def move(self, documents, redo=False):
        origin, dest = (self.redo_stack,self.undo_stack) if redo else (self.undo_stack,self.redo_stack)
        if not origin:
            return False
        dest.append(self.snapshot(documents))
        for doc,state in zip(documents,origin.pop()):
            doc.layers,doc.image,doc.original,doc.mask,doc.records = state
        return True


def validate_records(records):
    if not isinstance(records,list) or len(records)>1000:raise ValueError('识别行数超过 1000。')
    seen=set()
    for row in records:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'] or row['id'] in seen:raise ValueError('识别行编号无效。')
        seen.add(row['id'])
        for key in ('source','target'):
            if not isinstance(row.get(key),str) or len(row[key])>MAX_TEXT:raise ValueError('识别文本无效。')
        if 'recognized' in row and (not isinstance(row['recognized'],str) or len(row['recognized'])>MAX_TEXT):raise ValueError('初始识别文本无效。')
        score=row.get('score',0)
        if not isinstance(score,(int,float)) or not math.isfinite(score) or not 0<=score<=1:raise ValueError('识别置信度无效。')
        box=row.get('box',[])
        if len(box)!=4 or any(len(p)!=2 or any(not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=1 for v in p) for p in box):raise ValueError('识别框无效。')
        if not convex(box):raise ValueError('识别框不能折叠。')
        if 'style' in row:
            style=row['style']
            if not isinstance(style,dict):raise ValueError('样式记录无效。')
            Layer(**{k:style[k] for k in ('font','size','color','outline','outline_color','angle') if k in style}).validate()
            score=style.get('match_score',0)
            if not isinstance(score,(float,int)) or not math.isfinite(score) or not 0<=score<=1:raise ValueError('样式近似分数无效。')
