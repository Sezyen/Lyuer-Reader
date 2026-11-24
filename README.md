# Lyuer-Reader
# MienArch Lyuer — 技术说明文档

**EPUB / PDF / TXT 电子书阅读器套件**
版权所有 (c) MienArch | 面包图书馆 | 2026
许可证：**GNU GPL v3.0 or later**
---

## 1. 项目概览

MienArch Lyuer 是一个基于 **PySide6 / Qt** 的桌面电子书阅读器套件，支持
EPUB、PDF、TXT 三种格式，包含阅读器主程序与 calibre 式书库管理器两个入口，
二者共用同一份 SQLite 书库。

```
┌────────────────────────────────────────────────────┐
│  lyuer_main_1.6.3.0.py   主程序（阅读器入口）       │
│    ├── lyuer_epubviewer.py    EPUB 引擎 (WebEngine) │
│    ├── lyuer_pdfviewer.py     PDF 引擎 (QtPdf)      │
│    └── lyuer_database.py      SQLite 书库           │
│                                                    │
│  lyuer_manager.py        书库管理器（calibre 式）   │
│    └── lyuer_database.py      （共用同一份数据库）  │
└────────────────────────────────────────────────────┘
```

| 脚本 | 当前版本 | 角色 | 关键依赖 |
|---|---|---|---|
| `lyuer_main_1.6.3.0.py` | v1.6.3.0 | 主程序：UI 装配、书库树、TXT 阅读器、后台扫描/解析线程 | PySide6(+WebEngine/QtPdf)、ebooklib、bs4、lxml、PyMuPDF |
| `lyuer_epubviewer.py` | v1.0.4 | EPUB 解析（zip 优先 / ebooklib 兜底）+ Qt WebEngine 渲染 | PySide6-WebEngine、lxml、bs4 |
| `lyuer_pdfviewer.py` | v2.1.2 | QtPdf 连续滚动渲染 + PyMuPDF 元数据/书签解析 | PySide6(QtPdf/QtPdfWidgets)、PyMuPDF(可选) |
| `lyuer_database.py` | v1.4.1 | SQLite 持久化：书籍记录、阅读进度、封面 BLOB、设置 | 仅标准库（Qt-free） |
| `lyuer_manager.py` | v1.1.1 | 书库管理器：封面网格、筛选树、元数据编辑、批量封面提取 | PySide6(仅 Widgets)、PyMuPDF(可选) |

> 模块间通过**鸭子类型**解耦：`EpubChapter / TxtChapter / PdfChapter` 保持相同
> 字段（`id/title/content/order/file_name/anchor/page_start/page_end`），
> 主程序以同一套 `load_chapter()` 逻辑驱动三种格式。

---

## 2. 运行环境

- Python ≥ 3.10（使用 `X | Y` 联合类型注解）
- 支持 Windows / macOS / Linux（PySide6 跨平台；文件定位使用
  `os.startfile / open / xdg-open` 三分支）
- 触屏设备可完整使用捏合缩放、滑动翻页等手势

### 2.1 依赖安装

```bash
pip install PySide6 PySide6-WebEngine ebooklib beautifulsoup4 lxml PyMuPDF
```

- **PySide6-WebEngine**：EPUB 渲染必需（缺失时 EPUB 功能禁用，程序其余部分可用）
- **PyMuPDF**：PDF 元数据/封面解析与扫描必需（缺失时无法扫描/打开 PDF，
  每扫描周期仅提示一次）
- **QtPdf**：随官方 PySide6 wheel 自带，无需单独安装

主程序启动时做依赖自检，缺失时弹出图形化/终端提示并退出；
`lyuer_database.py` 保持 **Qt-free**，可脱离 GUI 独立使用。

---

## 3. 主程序架构（lyuer_main）

### 3.1 三栏式主窗口

```
┌──────────┬──────────┬────────────────────┐
│ 书库面板  │ 章节面板  │ 阅读工具栏 + 视图栈  │
│ (树/表格) │ (列表)   │ EPUB/TXT/PDF/加载页  │
└──────────┴──────────┴────────────────────┘
```

- **书库面板**：五列（文件名/书名/作者/修改日期/大小），自定义表头
  `SortableHeaderView` 高亮排序列并绘制 ▲/▼；支持点击表头排序（大小列按
  字节数比较，修正字典序 `9 MB > 10 MB` 的错误）、行内筛选、右键菜单、
  触屏滑动 + 轻点选中。
- **章节面板**：触屏滑动 + 轻点；点击即跳转章节。
- **视图栈 `QStackedWidget`**：`EpubViewerWidget → TxtReaderWidget →
  PdfViewerWidget → LoadingWidget`，按当前格式切换。

### 3.2 阅读偏好系统

统一字段：`font_size / theme(dark·light·sepia·mien) / custom_bg / custom_fg /
custom_font_western / custom_font_chinese / text_bold`，经
`_apply_reader_preferences()` 同步到 EPUB/TXT 阅读器；PDF 模式下字体/主题
控件整体禁用，`A±` 映射为缩放。

配置持久化在 `<应用目录>/.config/Lyuer_config.json`（旧版 QSettings 自动
迁移一次）。v1.6.3.0 修复：`apply_theme` 恢复主题下拉框时
`blockSignals(True)`，避免启动即清空用户自定义颜色。

### 3.3 后台线程模型

| 线程类 | 用途 | 通信 |
|---|---|---|
| `ScanThread` | 扫描关联目录、读取元数据入库 | `file_found / scan_finished / scan_error` 信号 |
| `BookLoadWorker` | 大文件 EPUB/TXT/PDF 解析 | `loaded(book, ext) / failed(msg)` 信号 |
| `LoadingWidget` | 解析期间转圈占位（>300ms 才显示，防闪烁） | — |

解析期间窗口不卡死；退出时 `wait(5000)` 后兜底 `terminate()`（v1.6.3.0）。

### 3.4 交互细节

- **键盘**：`←/→` 翻章（PDF 翻页）、`↑/↓` 滚动一页、`Ctrl+←/→` 翻章、
  `Ctrl+=/Ctrl+-` 字号；输入框聚焦时方向键守卫（`_arrow_guard`）不触发翻页。
- **拖拽**：窗口与 TXT 阅读区均接受 `.epub/.pdf/.txt` 拖入；支持命令行
  传入路径直接打开。
- **触屏**：捏合 → 字号步进（PDF 缩放）；左右快滑 → 翻章/翻页；
  `TouchCancel` 不误判为抬手；快滑翻页要求 <350ms 且累计滚动 <40px。

---

## 4. EPUB 引擎（lyuer_epubviewer）

### 4.1 解析层 `EpubParser`

- **双通道**：`zipfile` 直读（container.xml → OPF → spine/manifest/NCX/NAV）
  优先，`ebooklib` 兜底；
- **编码**：BOM → XML/HTML 声明 → 编码候选链（utf-8/gb18030/big5/…）；
- **切章**：NCX/NAV 目录锚点切分 → 标题（h1-h3）切分 → 整文件兜底，
  前置内容（扉页等）独立成章，封面/前置文件自动剔除；
- **资源**：正文 HTML 中的图片引用统一改写为 **data URI**（规避
  Qt WebEngine 的本地文件访问限制，书本自包含）。

### 4.2 渲染层 `EpubViewerWidget`（QWebEngineView）

- 正文包一层阅读样式模板（主题色、字号、行高、字体族、加粗），base URL
  使用保留域名 `https://lyuer.invalid/lyuer-epub/`，链接点击由
  `_EpubWebPage.acceptNavigationRequest` 拦截转为 `linkActivated` 信号；
- **关键修复链**：v1.0.1 剥离 base 前缀使目录链接可匹配；v1.0.3 延迟信号
  到事件循环下一轮（避免 `setHtml` 重入触发 WebEngine 断言闪退）+ 文件
  匹配即记 fallback + 大小写归一化；v1.0.4 滚动恢复异常时重启定时器重试
  （40ms 轮询直至成功），杜绝「失败一次即静默丢失」；
- **进度恢复**：`set_scroll_ratio / scroll_to_anchor` 统一走
  `_pending_* + QTimer` 单发重试队列，规避 `setHtml` 异步装载竞态；
- **触屏**：接管 `PinchGesture` 映射 `fontZoomRequested(±1)`，并禁用
  WebEngine 内置页面缩放（防手势弹跳）。

---

## 5. PDF 引擎（lyuer_pdfviewer）

- **渲染**：QtPdf `QPdfView`（MultiPage 连续滚动 + FitToWidth），
  `SelectablePdfView` 扩展文本选取（鼠标拖拽 + 自动滚动 + 高亮绘制）、
  缩放请求、触屏手势；
- **书签**：`QPdfBookmarkModel` 侧栏，翻页自动同步高亮，兼容不同 PySide6
  版本的 Role 枚举命名（`_resolve_bookmark_role`）；
- **定位**：`go_to_page()` 走 `pageNavigator().jump()` + 按 `_layout`
  几何手动对齐滚动条（MultiPage 下 jump 不一定驱动滚动条）；
  **v2.1.2** 起延迟闭包绑定装载时文档路径，旧文档遗留任务在新文档上
  自动失效（防切换 PDF 后被旧定时器误翻页）；
- **章节**：PyMuPDF `get_toc()` 转为 `PdfChapter(page_start/page_end)`，
  无书签时退化为「全文 N 页」单章；滚动换页经 `pageChanged` 反推章节
  并持久化进度。

---

## 6. 书库数据库（lyuer_database）

- 文件：`<数据库目录>/lyuer_database.sqlite3`，WAL 模式，
  `PRAGMA user_version` 迁移（当前 **schema v2**：v1.4.0 新增 `cover BLOB`、
  `comments TEXT`，旧库 `ALTER TABLE` 无损升级）；
- `BookRecord` dataclass 覆盖：路径/标题/作者/格式/大小/评分/丛书/出版社/
  标识符/标签/封面/简介/阅读进度（章节 + 滚动比例）/缺失标记/时间戳；
- API：CRUD、`search_books`（**LIKE 通配符转义**，`%`/`_` 按字面匹配）、
  `set_cover / set_comments / update_metadata`（calibre 式增量更新，
  `meta` 合并保留阅读进度）、`set_reading_position / touch_opened`；
- 多目录关联列表持久化在主程序 JSON 配置中；数据库目录可通过菜单迁移
  （WAL checkpoint 后 `copy2`，副产物 `-wal/-shm` 不复制）。

---

## 7. 书库管理器（lyuer_manager）

- 与主程序**共用同一份 SQLite 库**；calibre 式「数据库即单一事实来源」，
  编辑即写库；
- **左侧筛选树**：全部书籍 / 作者 / 丛书 / 出版社 / 文件类型 / 评分 / 标签，
  均带计数；**中央封面网格**：EPUB 提取封面图、PDF 渲染首页缩略图、
  无封面生成「牛皮纸」占位图（书名种子化纹理）；
- **元数据编辑**：书名/作者/丛书/序号/出版社/评分/标签/ISBN/简介；
- **封面批量提取**：按钮右键菜单「提取全部 / 仅补齐缺失」；
- **打开方式**：调用 `lyuer_main*.py/.exe`（自动定位）、自选程序（记忆）、
  系统默认程序；
- **窗口激活同步**：`_sig()` 书库签名（v1.1.1 补齐评分/丛书/标签/简介等
  全部字段）比对后增量重载，避免缓存滞后或频繁闪烁。

---

## 8. 已知设计取舍

1. EPUB 页内脚本在 `about:blank` 派生页面运行——JavaScript 必须开启
   （滚动恢复/锚点依赖），已禁用本地/远程文件访问、插件与超链接审计，
   风险可控；
2. 大文件 TXT 按 30k 字符切块兜底，章节正则只识别常见网文/出版目录格式；
3. PDF 封面首屏渲染依赖 PyMuPDF，与 QtPdf 渲染引擎解耦——解析失败时
   PDF 仍可阅读，仅无封面；
4. 数据库凭路径 `normalize_path`（abspath + normcase）去重，跨盘符/大小写
   差异平台行为以本机为准。

---

## 9. 文件清单

```
lyuer_main_1.6.3.0.py   # 主程序入口（python lyuer_main_1.6.3.0.py [文件]）
lyuer_epubviewer.py     # EPUB 解析 + WebEngine 阅读器（须在 QApplication 前导入）
lyuer_pdfviewer.py      # QtPdf 阅读面板 + PyMuPDF 解析
lyuer_database.py       # SQLite 书库（Qt-free，可独立导入）
lyuer_manager.py        # 书库管理器入口
lyuer.png / Lyuer.png   # 图标（onedir 打包放 _internal/，开发放脚本同目录）
.config/Lyuer_config.json        # 主程序配置（自动创建）
lyuer_database/lyuer_database.sqlite3  # 书库（自动创建）
```

---

MienArch Lyuer © 2026 MienArch | 面包图书馆 
