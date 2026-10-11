"""Pixel-based translation, centering and geometry-only snapping (no raster edits)."""
import copy
import math
if __package__:from . import model as m
else:import model as m


def translated(layer, size, dx, dy):
    if not all(math.isfinite(v) for v in (dx, dy)):
        raise ValueError('位移必须为有限数值。')
    result = copy.deepcopy(layer)
    dx, dy = dx/size[0], dy/size[1]
    result.x += dx
    result.y += dy
    if result.quad:
        result.quad = [(x+dx, y+dy) for x,y in result.quad]
    result.validate()
    return result


def bounds(layer, size):
    pts = m.corners(layer, size)
    return min(x for x,y in pts), min(y for x,y in pts), max(x for x,y in pts), max(y for x,y in pts)


def centered(layer, size, axis='both'):
    if axis not in ('x','y','both','text'):
        raise ValueError('居中方式必须为 x、y、both 或 text。')
    if axis == 'text':
        result = copy.deepcopy(layer)
        result.align = 'center'
        return result
    left, top, right, bottom = bounds(layer, size)
    return translated(layer, size, (size[0]-left-right)/2 if axis in ('x','both') else 0,
                      (size[1]-top-bottom)/2 if axis in ('y','both') else 0)


def snap(layer, size, others=(), tolerance=6):
    """Snap parallel polygon edges and centers, incl. mirrored centers about canvas axes.

    Tolerance is document pixels; UI converts from display pixels. Invisible layers
    are not targets. No detection of edges baked into the background image.
    """
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError('无效的吸附距离。')
    w,h=size
    polygon=m.corners(layer,size)
    targets=[[(0,0),(w,0),(w,h),(0,h)]]
    targets += [m.corners(other,size) for other in others if other.id!=layer.id and other.visible]
    candidates=[]
    # Each candidate is a normal, a required distance along it, and a guide segment.
    for target in targets:
        for i in range(4):
            a,b=target[i],target[(i+1)%4]
            length=math.dist(a,b)
            if length<1e-6:continue
            ux,uy=(b[0]-a[0])/length,(b[1]-a[1])/length
            nx,ny=-uy,ux
            for j in range(4):
                c,d=polygon[j],polygon[(j+1)%4]
                span=math.dist(c,d)
                if span<1e-6:continue
                if abs(ux*(d[1]-c[1])-uy*(d[0]-c[0]))/span>math.sin(math.radians(2)):continue
                projection=sorted(((p[0]-a[0])*ux+(p[1]-a[1])*uy for p in (c,d)))
                if projection[1]<-tolerance or projection[0]>length+tolerance:continue
                distance=((a[0]-(c[0]+d[0])/2)*nx+(a[1]-(c[1]+d[1])/2)*ny)
                if abs(distance)<=tolerance:candidates.append((nx,ny,distance,(a,b)))
    left,top,right,bottom=bounds(layer,size)
    cx,cy=(left+right)/2,(top+bottom)/2
    centers=[(w/2,h/2)]
    for target in targets[1:]:
        tx=(min(p[0] for p in target)+max(p[0] for p in target))/2
        ty=(min(p[1] for p in target)+max(p[1] for p in target))/2
        centers.extend([(tx,ty),(w-tx,h-ty)])
    for tx,ty in centers:
        if abs(tx-cx)<=tolerance:candidates.append((1,0,tx-cx,((tx,0),(tx,h))))
        if abs(ty-cy)<=tolerance:candidates.append((0,1,ty-cy,((0,ty),(w,ty))))
    if not candidates:return copy.deepcopy(layer),[]
    candidates.sort(key=lambda c:abs(c[2]))
    nx,ny,d,guide=candidates[0]
    dx,dy=nx*d,ny*d
    guides=[guide]
    # Solve a second independent constraint; don't let near-parallel guides cause jumps.
    for mx,my,e,second in candidates[1:]:
        det=nx*my-ny*mx
        if abs(det)<.5:continue
        sx,sy=(d*my-ny*e)/det,(nx*e-d*mx)/det
        if math.hypot(sx,sy)<=tolerance*math.sqrt(2)+1e-7:
            dx,dy=sx,sy;guides.append(second);break
    return translated(layer,size,dx,dy),guides
