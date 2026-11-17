"""侧边栏浏览器（模型能用的那种）。

**懒加载是硬约束**（docs/browser-design.md §11.7）：本包及 tools_browser 不在模块顶层
import QtWebEngine*。WebEngine 的导入、profile、page 都推迟到第一次真正用到浏览器
（browser_open / 打开浏览器面板）时才创建——不用浏览器就不存在，冷启动/内存不劣化。

纯逻辑（security.py 网址/来源/有后果判定、tree.py diff/围栏/上限）不依赖 Qt，可单测；
page.py / bridge.py 才碰 WebEngine，由调用方在首次使用时惰性导入。
"""