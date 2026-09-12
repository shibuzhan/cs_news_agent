# 官方 RSS 更新（公告/版本）的配图风格

供 `app/services/image_brief.py` 按来源读取：`## 风格池` 是彼此可替换的完整视觉方案（介质、纸张、
光线、视角、色调），文章级各取一条；`## 实物池` 是候选主体，每张图各取一个。两处都只返回**编号**：
风格由规划模型按文章内容挑，实物按每张图挑，越界或模型不可用时按草稿 ID 哈希轮换。因此同一来源的
不同文章在风格与实物上都会变化，而画面始终落在下面这些已审核过的组合里。

**路线：印刷与邮包质感。** 公告类内容用平面设计的"印出来 / 寄出去"质感替代 3D 渲染：
专色、纸纹、轻微套印偏移，天然不带 AI 的高光与渐变。

## 风格池

1. Two-colour risograph print on off-white paper, flat shapes with visible paper grain and slight ink misregistration, no gradients, even flat light, straight-on view. Palette: deep ink green and warm grey.
2. Two-colour risograph print with a heavier ink lay, coarse paper grain and visible roller marks, straight-on view. Palette: cobalt blue and gold ochre.
3. Single-ink letterpress print on thick cotton paper, deep debossed edges catching the light, straight-on view. Palette: charcoal ink on cream.
4. Screen print with slight edge bleed and a hand-pulled feel, flat shapes, tiny ink gaps, straight-on view. Palette: vermilion and off-white.
5. Photocopied zine print, high-contrast flat shapes with faint toner speckle and one soft fold line, straight-on view. Palette: pure black on newsprint white.
6. Stamped ink print on kraft paper, visible fibre and slight ink spread, straight-on view. Palette: kraft brown and black ink.
7. Blueprint-style cyanotype print, white flat shapes on a deep blue ground with paper texture, straight-on view. Palette: prussian blue and white.
8. Halftone newsprint print, coarse dot screen over flat shapes, slight paper yellowing, straight-on view. Palette: black dots on aged white.
9. Flat screen print on tracing paper, translucent overlaps creating a third tone, soft even light, straight-on view. Palette: teal and warm grey.
10. Rubber-stamp print on off-white card, uneven ink coverage and crisp edges, subtle paper grain, straight-on view. Palette: violet ink and off-white.
11. Two-colour risograph print with a deliberate offset shadow of the same shape, strong paper grain, straight-on view. Palette: ink black and fluorescent pink.
12. Flat screen print on recycled grey board, muted ink absorption, no gradients, straight-on view. Palette: deep ink green and slate.

## 实物池

每条以 `[类别]` 开头：同一篇文章的多张插图会避开同一类别，因此每张图的主体在类别上也是分开的。

1. [parcel] a flat silhouette of a shipping box beside a folded paper tag
2. [stack] overlapping flat rectangles and one circle suggesting stacked sheets
3. [timeline] concentric flat arcs suggesting a timeline
4. [grid] a flat grid of small squares with two squares filled solid
5. [letter] a flat silhouette of a folded paper letter with a round seal
6. [cards] a flat stack of blank cards with one card offset to the side
7. [mailer] a flat silhouette of a cardboard envelope with a string closure
8. [stamps] overlapping flat circles suggesting layered stamps
9. [bag] a flat silhouette of a paper bag with a folded top edge
10. [tags] three blank tags hanging on a string
11. [tube] a flat silhouette of a cardboard tube with a paper band
12. [fold] two nested squares joined by a diagonal fold line
13. [mailer] a flat silhouette of a kraft mailer with a tuck flap
14. [stamp] a flat rubber stamp and its rectangular impression block
15. [labels] a flat sheet of peel-off blank labels with one label lifted at the corner
16. [archive] a flat archive box drawn open with blank folders standing inside
17. [binding] a flat silhouette of two binder rings holding a small stack of cards
18. [clips] a flat cluster of paper clips fanned across the composition
19. [tickets] a flat silhouette of a ticket roll with one ticket torn free
20. [forms] a flat carbon-copy form pad with a folded corner
21. [envelopes] a flat set of stacked envelopes seen edge-on, offset like a staircase
22. [fold] a flat silhouette of a chevron-shaped paper fold pointing right
23. [grid] a flat composition of scattered small squares settling into a row
24. [locker] a flat silhouette of a parcel locker door with a round handle
