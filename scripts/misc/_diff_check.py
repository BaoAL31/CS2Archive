from PIL import Image
import os

def diff(a, b):
    ia = Image.open(a).convert('L')
    ib = Image.open(b).convert('L')
    return sum(abs(x-y) for x,y in zip(ia.getdata(), ib.getdata()))/len(ia.getdata())

print('op_with_enable animation check:')
times = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5]
for t in times:
    p = f'D:/Projects/CS2Archive/filter_test/op_with_enable_t{t}.png'
    if not os.path.exists(p):
        continue
    img = Image.open(p).convert('L')
    pix = list(img.getdata())
    yavg = sum(pix)/len(pix)
    print(f'  t={t}: yavg={yavg:.2f}')

print('op_with_enable pairwise diffs:')
pairs = [(1.5,2.0), (2.0,2.5), (2.5,3.0), (3.0,3.5), (3.5,4.0), (4.0,4.5)]
for a, b in pairs:
    pa = f'D:/Projects/CS2Archive/filter_test/op_with_enable_t{a}.png'
    pb = f'D:/Projects/CS2Archive/filter_test/op_with_enable_t{b}.png'
    print(f'  {a} vs {b}: diff={diff(pa,pb):.2f}')

print()
print('LIVE overlay animation check:')
for t in [28.5, 29.0, 30.0, 31.0, 32.0, 33.0]:
    p = f'D:/Projects/CS2Archive/renders/pov-furia-vs-falcons-m3-inferno_76561198041683378/proof/he2_t{t}.png'
    if not os.path.exists(p):
        continue
    img = Image.open(p).convert('L')
    pix = list(img.getdata())
    yavg = sum(pix)/len(pix)
    print(f'  t={t}: yavg={yavg:.2f}')

print('LIVE pairwise diffs:')
pairs = [(28.5,29.0), (29.0,30.0), (30.0,31.0), (31.0,32.0), (32.0,33.0)]
for a, b in pairs:
    pa = f'D:/Projects/CS2Archive/renders/pov-furia-vs-falcons-m3-inferno_76561198041683378/proof/he2_t{a}.png'
    pb = f'D:/Projects/CS2Archive/renders/pov-furia-vs-falcons-m3-inferno_76561198041683378/proof/he2_t{b}.png'
    print(f'  {a} vs {b}: diff={diff(pa,pb):.2f}')
