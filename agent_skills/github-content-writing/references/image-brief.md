# GitHub 项目的配图风格

供 `app/services/image_brief.py` 按来源读取：`## 风格池` 是彼此可替换的完整视觉方案（介质、光线、
材质、镜头、色调），文章级各取一条；`## 实物池` 是候选主体，每张图各取一个。两处都只返回**编号**：
风格由规划模型按文章内容挑，实物按每张图挑，越界或模型不可用时按草稿 ID 哈希轮换。因此同一来源的
不同文章在风格与实物上都会变化，而画面始终落在下面这些已审核过的组合里。

**路线：科技与开源内容的实物摄影。** 用真实硬件、工程工具与桌面物件建立"被拍下来"的可信感，
替换 isometric 3D 模块图与抽象数据流这类最容易显得像 AI 的构图。

## 风格池

1. Editorial still-life photograph on a seamless pale paper backdrop, hard directional daylight from the upper left casting one crisp shadow, matte materials, deep negative space, 50mm, f/4, fine film grain. Palette: off-white, graphite grey, one muted amber accent.
2. Documentary available-light photograph at a tidy workbench, cool overcast window light, shallow depth of field, honest wear on the surfaces, 35mm, f/2, subtle grain. Palette: cool grey, steel blue, one warm indicator.
3. Macro detail photograph on brushed aluminium, raking hard light emphasising texture, very shallow depth of field, 100mm macro, f/4, fine grain. Palette: silver grey, black, one teal accent.
4. Top-down flat-lay photograph on matte paper, even diffused daylight with no harsh shadows, everything squared to the frame, 35mm, f/5.6, gentle grain. Palette: off-white, ink green, sand.
5. Low-angle photograph on a dark desk at night, a single warm desk lamp as the only light source, deep shadows and one bright edge, 50mm, f/2, visible grain. Palette: charcoal, amber, deep blue.
6. Wide environmental photograph of a quiet home-office corner, soft daylight through a half-closed blind, muted colours, generous empty wall space, 24mm, f/4, fine grain. Palette: warm grey, pale wood, one dusty blue.
7. Still-life photograph on a raw concrete surface, cool fluorescent light from above, crisp micro-shadows, 50mm, f/5.6, subtle grain. Palette: concrete grey, off-white, one signal orange accent.
8. Photograph on a linen cloth backdrop, soft directional light with gentle falloff, tactile fabric texture, 85mm, f/2.8, fine grain. Palette: oat, warm grey, one rust accent.
9. Overhead photograph on a metal workbench, hard side light from a single window, visible dust and micro-scratches, 50mm, f/4, film grain. Palette: gunmetal, paper white, one acid green accent.
10. Close photograph on a glass shelf, cool window light with soft reflections and clean edges, 85mm, f/4, fine grain. Palette: pale blue-grey, white, one coral accent.
11. Studio photograph on a deep green seamless backdrop, one softbox from the side, controlled falloff into shadow, 85mm, f/5.6, very fine grain. Palette: deep green, off-white, brass.
12. Photograph on a walnut desk in late afternoon sun, long warm shadows, visible wood grain, 35mm, f/2.8, gentle grain. Palette: walnut brown, cream, one slate blue.

## 实物池

每条以 `[类别]` 开头：同一篇文章的多张插图会避开同一类别，因此每张图的主体在类别上也是分开的。

1. [desk] a mechanical keyboard seen at a low angle, matte keycaps
2. [cable] a coiled braided USB-C cable beside a small aluminium adapter on a matte desk
3. [desk] a laptop hinge and keyboard edge at a low angle, the screen is closed
4. [tools] a desk drawer organiser holding screws, adapters and a small screwdriver, seen from above
5. [power] a wall-mounted power strip with neatly bundled cables in a home-office corner
6. [board] a single-board computer with a small heatsink and a ribbon cable, no branding
7. [board] a graphics card with a large heatsink and two fans, seen at an angle, no branding
8. [tools] a soldering station with a brass wool tip cleaner and a coiled solder wire
9. [prototype] a spool of hook-up wire beside a breadboard with a few jumper leads
10. [robot] a small robot arm with exposed servo motors on a metal base
11. [printer] a 3D printer midway through a print, filament spool visible in the background
12. [case] an opened single-board computer case with loose screws in a shallow tray
13. [cooling] a stack of small cooling fans and a tube of thermal paste on a work mat
14. [rack] a rack of rack-mount screws and cage nuts in a divided tray
15. [network] a network switch with rows of status LEDs in a small home rack
16. [network] a handheld cable tester with a short patch cable coiled beside it
17. [audio] a pair of over-ear headphones resting on a closed laptop lid
18. [desk] a ceramic mug beside a small stack of blank sticky notes
19. [paper] a folded paper manual and a single screwdriver on a matte surface
20. [paper] a closed notebook with an elastic band and a pen resting on top
21. [packaging] a small cardboard box with a blank shipping label, still closed
22. [tools] a set of precision tweezers and a magnifier on a dark work mat
23. [audio] a desktop microphone on a boom arm beside a closed notebook
24. [storage] a stack of external solid-state drives with a short cable on a matte desk
