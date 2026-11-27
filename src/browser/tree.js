// 灵犀侧边栏浏览器：注入式可访问性文字树（生产版，从原型移植 + 省 token 规则）。
// DocumentCreation 早期注入、MainWorld、runsOnSubFrames。安装一次后调
// window.__lx.snapshot / find / click / type / act / readText / console / state / info。
// 设计依据 docs/browser-design.md §3.4/§3.5/§4.1/§6.1。
(() => {
  if (window.__lx) return "already-installed";

  const INTERACTIVE = new Set(["button", "link", "textbox", "searchbox", "checkbox", "radio",
    "combobox", "listbox", "option", "slider", "spinbutton", "switch", "menuitem",
    "menuitemcheckbox", "menuitemradio", "tab", "treeitem"]);
  const LANDMARK = new Set(["navigation", "main", "banner", "contentinfo", "complementary",
    "region", "form", "search"]);
  const TEXT_LEAF = new Set(["P", "LI", "TD", "TH", "PRE", "BLOCKQUOTE", "FIGCAPTION",
    "CAPTION", "DD", "DT", "H1", "H2", "H3", "H4", "H5", "H6"]);
  const SKIP_TAG = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "META",
    "LINK", "TITLE", "BASE", "SVG", "PATH"]);
  const DATA_STATES = new Set(["open", "closed", "checked", "unchecked", "indeterminate", "active",
    "inactive", "on", "off", "selected", "unselected", "expanded", "collapsed", "disabled"]);
  const NAME_MAX = 120;
  const WALK_NODE_CAP = 30000;
  const FOLD_LINK_LIMIT = 8;        // §3.5：contentinfo/banner 里同级链接超过这个数就折叠
  const FROM_CONTENT = new Set(["button", "link", "heading", "option", "menuitem",
    "menuitemcheckbox", "menuitemradio", "tab", "treeitem", "cell", "columnheader",
    "rowheader", "listitem", "checkbox", "radio", "switch"]);

  const refs = new Map();      // ref -> element
  const elRefs = new Map();    // element -> ref：同一文档内 ref 稳定复用，不清空不重排
  let refSeq = 0;
  let generation = 0;
  // 每文档唯一标识：换文档后旧 ref 一律失效（工具层比对签发时的 docId）
  const docId = Math.random().toString(36).slice(2, 10) + Date.now().toString(36);

  const clean = (s, max) => {
    s = (s || "").replace(/\s+/g, " ").trim();
    return s.length > max ? s.slice(0, max) + "…" : s;
  };

  // 密码类输入框：值一律不出快照、不回显（复审 #18）。用户在面板里亲手输的密码以前会
  // 以 [value="…"] 进文字树，发给模型服务商、落进会话历史。除了 type=password，
  // 浏览器自动填充用的这几种 autocomplete 也算；「显示密码」把 type 临时改成 text 的，
  // 见过一次是密码框就一直按密码框处理（seenSecret）。
  const SECRET_AUTOCOMPLETE = /(^|\s)(current-password|new-password|one-time-code|cc-number|cc-csc)(\s|$)/;
  const seenSecret = new WeakSet();
  function isSecret(el) {
    if (!el || el.tagName !== "INPUT") return false;
    if (seenSecret.has(el)) return true;
    const t = (el.type || "").toLowerCase();
    const ac = (el.getAttribute("autocomplete") || "").toLowerCase();
    if (t === "password" || SECRET_AUTOCOMPLETE.test(ac)) {
      seenSecret.add(el);
      return true;
    }
    return false;
  }

  function roleOf(el) {
    const explicit = (el.getAttribute("role") || "").trim().split(/\s+/)[0];
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === "A") return el.hasAttribute("href") ? "link" : "generic";
    if (tag === "BUTTON") return "button";
    if (tag === "SUMMARY") return "button";
    if (tag === "SELECT") return el.multiple || el.size > 1 ? "listbox" : "combobox";
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "IMG") return (el.getAttribute("alt") === "" ? "presentation" : "img");
    if (tag === "INPUT") {
      const t = (el.type || "text").toLowerCase();
      if (t === "hidden") return "hidden";
      if (t === "submit" || t === "button" || t === "reset" || t === "image") return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "range") return "slider";
      if (t === "number") return "spinbutton";
      if (t === "search") return "searchbox";
      return "textbox";
    }
    if (/^H[1-6]$/.test(tag)) return "heading";
    if (tag === "NAV") return "navigation";
    if (tag === "MAIN") return "main";
    if (tag === "HEADER") return "banner";
    if (tag === "FOOTER") return "contentinfo";
    if (tag === "ASIDE") return "complementary";
    if (tag === "FORM") return "form";
    if (tag === "SECTION") return (el.getAttribute("aria-label") || el.getAttribute("aria-labelledby")) ? "region" : "generic";
    if (tag === "ARTICLE") return "article";
    if (tag === "DIALOG") return "dialog";
    if (tag === "UL" || tag === "OL") return "list";
    if (tag === "LI") return "listitem";
    if (tag === "TABLE") return "table";
    if (tag === "TR") return "row";
    if (tag === "TD") return "cell";
    if (tag === "TH") return "columnheader";
    if (tag === "OPTION") return "option";
    if (tag === "DETAILS") return "group";
    // 可编辑区只有最外层（编辑宿主）算输入框：里面每个 <p> 也是 isContentEditable，
    // 以前各算一个无名 textbox、各占一个 ref，正文反而看不到（复审 #11）
    if (el.isContentEditable && !(el.parentElement && el.parentElement.isContentEditable))
      return "textbox";
    if (el.hasAttribute("onclick")) return "button";
    return "generic";
  }

  // 可编辑区旁边的表单标签：往上最多 8 层（antd 表单里编辑器到标签那一行隔着 7 层），找「不在它里面、for 没对上别的控件」的 <label>。
  // 只在这一层恰好一个这样的标签、而且除了它自己再没有别的可见输入控件时才算——两个编辑器
  // 共用一层时宁可不取名，也不能把标题的名字安到正文上
  const EDITABLE_SEL = "input:not([type=hidden]),textarea,select,[contenteditable=''],[contenteditable='true']";
  function orphanLabelFor(el) {
    let a = el.parentElement;
    for (let i = 0; a && a !== document.body && i < 8; i++, a = a.parentElement) {
      const labels = Array.from(a.querySelectorAll("label")).filter(l => {
        if (l.contains(el)) return false;
        const target = l.htmlFor ? document.getElementById(l.htmlFor) : null;
        return !target || target.contains(el);
      });
      if (!labels.length) continue;
      if (labels.length !== 1) return "";
      for (const c of a.querySelectorAll(EDITABLE_SEL)) {
        if (c === el || el.contains(c) || c.contains(el)) continue;
        if (visible(c)) return "";
      }
      return labels[0].textContent || "";
    }
    return "";
  }

  // 元素里「给人看的」文字：跳过表单控件自己的内容（下拉框的全部选项、文本域里的字）
  const CONTROL_TEXT_TAGS = new Set(["SELECT", "OPTION", "TEXTAREA", "SCRIPT", "STYLE", "TEMPLATE"]);
  function plainText(node) {
    let out = "";
    const walk = (n) => {
      if (n.nodeType === 3) { out += n.textContent; return; }
      if (n.nodeType !== 1 || CONTROL_TEXT_TAGS.has(n.tagName)) return;
      for (const c of n.childNodes) walk(c);
      if (n.tagName === "TD" || n.tagName === "TH" || n.tagName === "BR") out += " ";
    };
    walk(node);
    return out.replace(/\s+/g, " ").trim();
  }

  // ── 表格里没名字的输入格：按行列起名 ──
  // 实测（簿记模试）：几百个下拉框 / 输入框都没有名字，模型只能按「每行 4 格」数 ref，数错一格
  // 整行借贷就反了，回头核对也看不出哪格是哪格。表格本身写着答案：列头（借方科目 / 金額）、
  // 行首（現金 / 貸倒引当金）、外层表格的题号。只给本来没名字的控件用
  const TABLE_ROW_CAP = 200;
  const HAS_WORD = /[\p{L}\p{N}]/u;
  const AMOUNT = /^[\d,.\s]+$/;
  const DITTO = /^(〃|″|”|"|同上)$/;

  function hasControl(cell) {
    return !!cell.querySelector("input:not([type=hidden]),select,textarea");
  }

  // 表格的格子网格（处理 rowspan / colspan）：返回 { grid[r][c] = 格子, pos: 格子 -> {r, c} }
  function tableGrid(table) {
    const rows = Array.from(table.rows).filter(tr => tr.closest("table") === table);
    if (rows.length > TABLE_ROW_CAP) return null;
    const grid = [], pos = new Map();
    rows.forEach((tr, r) => {
      grid[r] = grid[r] || [];
      let c = 0;
      for (const cell of tr.cells) {
        while (grid[r][c]) c++;
        pos.set(cell, { r, c });
        const rs = Math.max(1, Math.min(cell.rowSpan || 1, rows.length - r));
        const cs = Math.max(1, Math.min(cell.colSpan || 1, 50));
        for (let i = 0; i < rs; i++)
          for (let j = 0; j < cs; j++) (grid[r + i] = grid[r + i] || [])[c + j] = cell;
        c += cs;
      }
    });
    return { grid, pos, rows };
  }

  // 像表头的一行：在 thead 里、全是 th，或者没有输入控件、也没有金额那样的数字格
  function headerLike(tr, cells) {
    if (tr.parentElement && tr.parentElement.tagName === "THEAD") return true;
    const own = Array.from(tr.cells);
    if (own.length && own.every(c => c.tagName === "TH")) return true;
    if (cells.some(hasControl)) return false;
    const texts = own.map(plainText).filter(Boolean);
    return texts.length > 0 && !texts.some(t => AMOUNT.test(t) && /\d/.test(t));
  }

  function usableLabel(t) {
    return !!t && HAS_WORD.test(t) && !(AMOUNT.test(t) && t.replace(/\D/g, "").length > 3) && t.length <= 40;
  }

  function tableContextName(el) {
    const cell = el.closest("td,th");
    const table = cell && cell.closest("table");
    if (!table) return "";
    const g = tableGrid(table);
    if (!g || !g.pos.has(cell)) return "";
    const { r, c } = g.pos.get(cell);
    const parts = [];

    // 外层表格（题目表里套着答题表）：外层这一行的第一格是短的编号就带上（「10」）
    const outerCell = table.parentElement && table.parentElement.closest("td,th");
    if (outerCell && outerCell.parentElement) {
      const first = outerCell.parentElement.cells[0];
      if (first && first !== outerCell && !first.contains(table)) {
        const t = plainText(first);
        if (t && t.length <= 6 && HAS_WORD.test(t)) parts.push(t);
      }
    }

    // 行名：控件自己格子里的字（「当期純[  ]」「雑[  ]」），否则往左找最近一个有字的格子
    // （格子里另有输入框也行：「雑[  ] | [  ]」右边那格也叫「雑」）。「〃」是同上：
    // 往上找同一列的字（T 字账右半边「〃」指的是右边上一行的日期，不是左半边的）
    let rowLabel = "";
    const ownText = plainText(cell);
    if (usableLabel(ownText)) rowLabel = ownText;
    for (let cc = c - 1; !rowLabel && cc >= 0; cc--) {
      const left = g.grid[r][cc];
      if (!left || left === cell) continue;
      const t = plainText(left);
      if (DITTO.test(t)) {
        for (let rr = r - 1; rr >= 0 && !rowLabel; rr--) {
          const up = g.grid[rr] && g.grid[rr][cc];
          const ut = up ? plainText(up) : "";
          if (usableLabel(ut)) rowLabel = ut;
          else if (!DITTO.test(ut)) break;
        }
        break;
      }
      if (usableLabel(t)) rowLabel = t;
    }

    // 列名：往上最近一行像表头的，同一列那一格的字；列名重复（借方「金額」、贷方「金額」）时
    // 带上左边最近的另一个列名
    let colLabel = "", headerRow = -1;
    for (let rr = r - 1; rr >= 0; rr--) {
      const rowCells = (g.grid[rr] || []).filter(Boolean);
      if (!headerLike(g.rows[rr], rowCells)) continue;
      const h = g.grid[rr][c];
      const t = h ? plainText(h) : "";
      if (!t || !HAS_WORD.test(t)) break;
      colLabel = t;
      headerRow = rr;
      const texts = (g.grid[rr] || []).map(x => (x ? plainText(x) : ""));
      if (texts.filter(x => x === t).length > 1) {
        for (let cc = c - 1; cc >= 0; cc--) {
          const p = texts[cc];
          if (p && p !== t) { colLabel = p + " " + t; break; }
        }
      }
      break;
    }

    // 没有行名时用第几行（表头以下、有控件的行里数）
    if (!rowLabel && colLabel) {
      let n = 0, total = 0;
      for (let rr = headerRow + 1; rr < g.rows.length; rr++) {
        if (Array.from(g.rows[rr].cells).some(hasControl)) {
          total++;
          if (rr <= r) n = total;
        }
      }
      if (total > 1) rowLabel = "第" + n + "行";
    }
    if (rowLabel) parts.push(rowLabel);
    if (colLabel && colLabel !== rowLabel) parts.push(colLabel);
    // 只有外层编号、没有本表的行列信息：不算名字
    if (!rowLabel && !colLabel) return "";
    return parts.join(" · ");
  }

  function nameOf(el, role, useText) {
    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      const parts = labelledby.split(/\s+/).map(id => {
        const t = document.getElementById(id);
        return t ? t.textContent : "";
      }).filter(Boolean);
      if (parts.length) return clean(parts.join(" "), NAME_MAX);
    }
    const label = el.getAttribute("aria-label");
    if (label && label.trim()) return clean(label, NAME_MAX);
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
      // 标签文字不算控件自己的内容：<label><select>…</select></label> 这种包法，以前下拉框的
      // 名字成了「現金 立替金 買掛金…」（全部选项）。只剩选项文字的标签当作没有标签
      if (el.labels && el.labels.length) {
        const lab = clean(Array.from(el.labels).map(plainText).join(" "), NAME_MAX);
        if (lab) return lab;
      }
      const ph = el.getAttribute("placeholder");
      if (ph) return clean(ph, NAME_MAX);
      const t = (el.type || "").toLowerCase();
      if (tag === "INPUT" && (t === "submit" || t === "button" || t === "reset"))
        return clean(el.value || t, NAME_MAX);
      if (el.title) return clean(el.title, NAME_MAX);
      const ctx = tableContextName(el);
      if (ctx) return clean(ctx, NAME_MAX);
    }
    if (tag === "IMG") return clean(el.getAttribute("alt") || el.title || imgFileName(el), NAME_MAX);
    if (role === "textbox" && el.isContentEditable) {
      // 富文本框（Quill / 自己包的编辑器组件）多半没有 aria-label：表单标签写着「内容」，可它的 for
      // 指向一个不存在的 id，读屏和快照里都只剩一个无名 textbox，模型分不清哪个是标题哪个是正文
      // （复审 #11，实测 antd 表单里两个 Quill）。取旁边那个没对上任何控件的标签，再不行用占位文字
      const lab = orphanLabelFor(el);
      if (lab) return clean(lab, NAME_MAX);
      const ph = el.getAttribute("aria-placeholder") || el.getAttribute("data-placeholder")
        || el.getAttribute("placeholder");
      return clean(ph || el.title || "", NAME_MAX);
    }
    if (!FROM_CONTENT.has(role) && !useText) return clean(el.title || "", NAME_MAX);
    const text = clean(el.textContent, NAME_MAX);
    if (text) return text;
    if (el.title) return clean(el.title, NAME_MAX);
    const img = el.querySelector && el.querySelector("img[alt]");
    if (img) return clean(img.getAttribute("alt"), NAME_MAX);
    return "";
  }

  // 没写 alt 的图（题目里的票据、截图）以前只剩一行无名的「- img」，模型分不清是哪张，
  // 只好拿旁边输入框的 ref 去截图（实测：簿记模试的納品書截成了一个空输入框）。
  // 用地址里的文件名当名字；data: / blob: 这种没有文件名的不给
  function imgFileName(el) {
    const src = el.currentSrc || el.getAttribute("src") || "";
    if (!src || /^(data|blob):/i.test(src)) return "";
    let seg = "";
    try {
      seg = new URL(src, location.href).pathname.split("/").filter(Boolean).pop() || "";
    } catch (e) {
      return "";
    }
    try { return decodeURIComponent(seg); } catch (e) { return seg; }
  }

  function visible(el) {
    if (el.getAttribute("aria-hidden") === "true") return false;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || cs.visibility === "collapse")
      return false;
    // 只给读屏软件的文字（sr-only：绝对定位、1×1、内容裁掉）屏幕上看不见，比如组件库的
    // aria-live 播报区：不进文字树，免得点一下选项就报出一行「变化」（复审 #4）。
    // 可交互的照留（antd Select 的搜索框是透明的，但能输入）
    if ((cs.position === "absolute" || cs.position === "fixed")
        && (cs.overflow === "hidden" || cs.clip !== "auto" || cs.clipPath !== "none")) {
      const r = el.getBoundingClientRect();
      if (r.width <= 1 && r.height <= 1 && !INTERACTIVE.has(roleOf(el))) return false;
    }
    return true;
  }

  // 读屏孪生体（复审 #8）：antd 这类组件库另 render 一份 0 面积的 role=option/listbox
  // 给读屏软件，屏幕上看不见、真人点不到、脚本点了也没用。只认这两个角色——别的自定义
  // 控件（slider/checkbox）可能只是没写样式才 0 面积，不能误杀。原生 <option>/<select>
  // 除外（系统弹窗里显示）
  function isA11yTwin(el, role) {
    if (role !== "option" && role !== "listbox") return false;
    if (el.tagName === "OPTION" || el.tagName === "SELECT") return false;
    const r = el.getBoundingClientRect();
    return r.width < 1 || r.height < 1;
  }

  // 没写角色但能点的元素（React/Vue 用 JS 绑定的 div/li、antd 下拉的可见选项，复审 #8）：
  // cursor:pointer 或带 tabindex 也发 ref。cursor 会继承：只认连续 pointer 链的最外层，
  // 选项的内容 div 也继承 pointer，不该各占一个 ref。返回要附在行尾的标记（无则空串）。
  function clickMarker(el) {
    let cs;
    try { cs = getComputedStyle(el); } catch (e) { return ""; }
    if (cs.cursor === "pointer") {
      const p = el.parentElement;
      if (p && p.tagName !== "BODY" && p.tagName !== "HTML") {
        try { if (getComputedStyle(p).cursor === "pointer") return ""; } catch (e) { /* 当最外层 */ }
      }
      return "[cursor=pointer]";
    }
    if (el.hasAttribute("tabindex") && el.tabIndex >= 0) return "[tabindex]";
    return "";
  }

  function pruneRefs() {
    // 只剔除已脱离页面的元素；活着的元素保留原 ref（root_ref/diff/find 快照不得改指向）
    for (const [id, el] of Array.from(refs)) {
      if (!el.isConnected) {
        refs.delete(id);
        elRefs.delete(el);
      }
    }
  }

  function assignRef(el) {
    const old = elRefs.get(el);
    if (old && refs.get(old) === el) return old;
    const id = "e" + (++refSeq);
    refs.set(id, el);
    elRefs.set(el, id);
    // ref 只留在隔离世界的 Map 里，不写成 DOM 属性（复审 #21）：以前打 data-lxref，
    // 会存 innerHTML 的编辑器 / CMS 会把它存进用户数据
    return id;
  }

  function lineFor(el, role, name, depth, withRef, click) {
    let line = "  ".repeat(depth) + "- " + role;
    if (name) line += " " + JSON.stringify(name);
    if (/^H[1-6]$/.test(el.tagName)) line += " [level=" + el.tagName[1] + "]";
    if (withRef) line += " [ref=" + assignRef(el) + "]";
    if (click) line += " " + click;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") line += " [disabled]";
    if (role === "checkbox" || role === "radio" || role === "switch" || role === "menuitemcheckbox"
        || role === "menuitemradio") {
      // 原生 <input> 看 checked / indeterminate；div[role=checkbox] 这类自定义控件没有 checked
      // 属性，要看 aria-checked——早先只读 el.checked，自定义复选框永远显示 unchecked（实测）
      let st;
      if (el.tagName === "INPUT") st = el.indeterminate ? "mixed" : (el.checked ? "checked" : "unchecked");
      else {
        const a = (el.getAttribute("aria-checked") || "").toLowerCase();
        st = a === "true" ? "checked" : a === "mixed" ? "mixed" : "unchecked";
      }
      line += " [" + st + "]";
    }
    const pressed = el.getAttribute("aria-pressed");
    if (pressed === "true") line += " [pressed]";
    else if (pressed === "mixed") line += " [pressed=mixed]";
    const exp = el.getAttribute("aria-expanded");
    if (exp !== null) line += " [expanded=" + exp + "]";
    if (role === "textbox" || role === "searchbox" || role === "combobox"
        || role === "spinbutton" || role === "slider") {
      // 数字框 / 滑块的值、可编辑区（富文本框）的正文也要看得到（复审 #7）
      let v = el.value || "";
      if (!v && el.isContentEditable) v = el.innerText || "";
      if (!v && (role === "spinbutton" || role === "slider")) v = el.getAttribute("aria-valuenow") || "";
      if (v && isSecret(el)) line += " [filled]";
      else if (v) line += " [value=" + JSON.stringify(clean(v, 50)) + "]";
    }
    if ((role === "option" && el.selected) || el.getAttribute("aria-selected") === "true")
      line += " [selected]";
    // 其余标准状态：规范里定死的有限几种，读一次就够，不随项目适配
    if (role === "button" && el.tagName === "SUMMARY" && exp === null && el.parentElement
        && el.parentElement.tagName === "DETAILS")
      line += " [expanded=" + (el.parentElement.open ? "true" : "false") + "]";
    const cur = el.getAttribute("aria-current");
    if (cur && cur !== "false") line += cur === "true" ? " [current]" : " [current=" + cur + "]";
    const sort = el.getAttribute("aria-sort");
    if (sort && sort !== "none") line += " [sort=" + sort + "]";
    if (el.getAttribute("aria-invalid") === "true") line += " [invalid]";
    if (el.required || el.getAttribute("aria-required") === "true") line += " [required]";
    if ((role === "textbox" || role === "searchbox" || role === "spinbutton")
        && (el.readOnly || el.getAttribute("aria-readonly") === "true")) line += " [readonly]";
    if (el.getAttribute("aria-busy") === "true") line += " [busy]";
    // Radix / shadcn 这类组件库的 data-state；上面的 aria 状态已经说了就不重复
    const ds = (el.getAttribute("data-state") || "").toLowerCase();
    if (ds && DATA_STATES.has(ds) && !/\[(checked|unchecked|mixed|pressed|selected|expanded=)/.test(line))
      line += " [state=" + ds + "]";
    // 删除线是「已完成」最常见的画法（待办清单）：纯样式、文字树本来看不见，点完会被当成没变化
    try {
      const deco = getComputedStyle(el).textDecorationLine || "";
      if (deco.indexOf("line-through") >= 0) line += " [strikethrough]";
    } catch (e) { /* 拿不到样式就不标 */ }
    return line;
  }

  function snapshot(opts) {
    opts = opts || {};
    const mode = opts.mode || "interactive";
    const maxDepth = opts.maxDepth != null ? opts.maxDepth : 12;
    const maxChars = opts.maxChars != null ? opts.maxChars : 12000;
    const t0 = performance.now();
    pruneRefs();
    // 编号跨文档单调递增（Python 侧喂种子）：换页后旧编号不会重新签发指向别的元素
    if (opts.__seed) refSeq = Math.max(refSeq, opts.__seed);
    let root = document.body;
    if (opts.rootRef) {
      const r = refs.get(opts.rootRef);
      if (!r || !r.isConnected) return { ok: false, error: "rootRef 不存在或已脱离页面（重新 snapshot）" };
      root = r;
    }
    generation++;
    if (!root) return { ok: false, error: "没有 body" };

    const lines = [];
    const state = { chars: 0, truncated: false, nodes: 0, kept: 0 };
    // §3.5 折叠状态：进入 contentinfo/banner 时限制链接数；收起的 combobox 只给选中项。
    let foldActive = false, foldLeft = 0, foldSkipped = 0;

    function emit(line) {
      state.chars += line.length + 1;
      if (state.chars > maxChars) { state.truncated = true; return false; }
      lines.push(line);
      state.kept++;
      return true;
    }

    function walk(el, depth) {
      if (state.truncated || state.nodes >= WALK_NODE_CAP) return false;
      state.nodes++;
      if (SKIP_TAG.has(el.tagName) || !visible(el)) return false;
      const role = roleOf(el);
      if (role === "hidden" || role === "presentation") return false;
      if (isA11yTwin(el, role)) {
        // 自己的行不进树（不发 ref），子元素照常遍历——0 宽容器里溢出的可见内容不剪
        for (const c of el.childNodes) if (c.nodeType === 1) walk(c, depth);
        return false;
      }

      // 页脚/横幅链接折叠：超额链接整棵跳过（链接子树通常无其它可交互元素）
      if (foldActive && role === "link") {
        if (foldLeft > 0) foldLeft--;
        else { foldSkipped++; return false; }
      }

      const isFoldLandmark = (role === "contentinfo" || role === "banner");
      const saved = isFoldLandmark ? { foldActive, foldLeft, foldSkipped } : null;
      if (isFoldLandmark) { foldActive = true; foldLeft = FOLD_LINK_LIMIT; foldSkipped = 0; }

      // 收起的 combobox（原生 select 非 multiple）：只遍历选中 option
      const collapsedSelect = (role === "combobox" && el.tagName === "SELECT"
        && !el.multiple && el.getAttribute("aria-expanded") !== "true");
      let selectSkipped = 0;

      try {
        let selfLine = null;
        const isInteractive = INTERACTIVE.has(role);
        const click = (!isInteractive && !LANDMARK.has(role) && role !== "heading" && role !== "img")
          ? clickMarker(el) : "";
        // 图也发 ref：browser_screenshot(ref=…) 直接截那张图看内容
        const withRef = isInteractive || LANDMARK.has(role) || role === "heading" || role === "img" || !!click;
        if (mode === "interactive") {
          if (isInteractive || LANDMARK.has(role) || role === "heading" || role === "img" || click)
            selfLine = lineFor(el, role, nameOf(el, role, !!click), depth, withRef, click);
        } else {
          if (role !== "generic" || click) selfLine = lineFor(el, role, nameOf(el, role, !!click), depth, withRef, click);
          else if (TEXT_LEAF.has(el.tagName) || (!el.children.length && el.textContent.trim())) {
            const t = clean(el.textContent, 200);
            if (t) selfLine = "  ".repeat(depth) + "- text: " + JSON.stringify(t);
          }
        }

        const lineIdx = lines.length;
        if (selfLine !== null && depth <= maxDepth) {
          if (!emit(selfLine)) return false;
        }
        const before = lines.length;
        const childDepth = selfLine !== null && depth <= maxDepth ? depth + 1 : depth;

        const walkChild = (c) => {
          if (collapsedSelect && c.tagName === "OPTION" && !c.selected) { selectSkipped++; return; }
          walk(c, childDepth);
        };
        // 裸文本节点（容器里混排的文字）只在 all 模式、且该元素自身没产出行时补，
        // 否则"带子元素的 div"里的文字会丢（diff 假阴性），也与 name 重复
        const walkNode = (c) => {
          if (c.nodeType === 3) {
            if (mode === "all" && selfLine === null) {
              const t = clean(c.textContent, 200);
              if (t) emit("  ".repeat(childDepth) + "- text: " + JSON.stringify(t));
            }
            return;
          }
          if (c.nodeType === 1) walkChild(c);
        };
        if (el.shadowRoot) for (const c of el.shadowRoot.childNodes) walkNode(c);
        for (const c of el.childNodes) walkNode(c);

        if (collapsedSelect && selectSkipped > 0 && depth <= maxDepth)
          emit("  ".repeat(childDepth) + "- text: " + JSON.stringify("…还有 " + selectSkipped + " 个选项（收起，用 browser_find 查）"));

        if (mode === "interactive" && selfLine !== null && lines.length === before
            && !isInteractive && !click && role !== "heading" && role !== "img") {
          lines.splice(lineIdx, 1);
          state.kept--;
        }
        return lines.length > lineIdx || (selfLine !== null && lines.length === before);
      } finally {
        if (isFoldLandmark) {
          if (foldSkipped > 0 && !state.truncated && depth <= maxDepth)
            emit("  ".repeat(depth + 1) + "- text: " + JSON.stringify("…还有 " + foldSkipped + " 个链接，用 browser_find 查"));
          foldActive = saved.foldActive; foldLeft = saved.foldLeft; foldSkipped = saved.foldSkipped;
        }
      }
    }

    walk(root, 0);
    const tree = lines.join("\n");
    return {
      ok: true, generation, mode, docId, nextSeq: refSeq,
      url: location.href, title: document.title,
      tree,
      stats: {
        chars: tree.length, lines: lines.length, nodesVisited: state.nodes,
        refs: refs.size, truncated: state.truncated,
        jsMs: Math.round((performance.now() - t0) * 100) / 100,
      },
    };
  }

  function resolve(ref) {
    const el = refs.get(ref);
    if (!el) return { ok: false, error: "ref 不存在: " + ref + "（页面变了就重新 snapshot）" };
    if (!el.isConnected) return { ok: false, error: "ref 已脱离页面: " + ref };
    return { ok: true, el };
  }

  // 可激活祖先：点按钮里的 img/span 会冒泡激活外层按钮/链接，有后果判定和确认卡
  // 都要按祖先判，否则点提交按钮的子元素就绕过了"有后果必问"
  function activatable(el) {
    let n = el;
    while (n && n.nodeType === 1) {
      const tag = n.tagName;
      const typ = (n.type || "").toLowerCase();
      if (tag === "BUTTON" || tag === "SUMMARY") return n;
      // label 会激活它关联的控件（for= 或包裹）：按控件判，否则点 label 里的 img
      // 就绕过"有后果必问"（label.control 覆盖两种写法；无关联控件按 label 本身）
      if (tag === "LABEL") return n.control || n;
      if (tag === "INPUT" && (typ === "submit" || typ === "image")) return n;
      if (tag === "A" && n.hasAttribute("href")) return n;
      if ((n.getAttribute("role") || "").trim() === "button") return n;
      if (n.hasAttribute("onclick")) return n;
      if (n.parentElement) n = n.parentElement;
      else {
        const root = n.getRootNode();
        n = root && root.host ? root.host : null;
      }
    }
    return el;
  }

  function formOf(el) {
    return el.form || (el.closest ? el.closest("form") : null);
  }

  // 表单的默认提交按钮（表单内回车时浏览器替用户点的那个）的名字：回车提交按它判有没有后果
  function defaultSubmitName(form) {
    const els = Array.from((form && form.elements) || []);
    const b = els.find(e => (e.tagName === "BUTTON" && (e.type || "submit") === "submit")
      || (e.tagName === "INPUT" && (e.type === "submit" || e.type === "image")));
    return b ? nameOf(b, roleOf(b), true) : "";
  }

  // 结构指纹：批准（info）与执行（click/type）之间元素被页面改过就拒绝
  function fingerprint(el) {
    const a = activatable(el);
    const form = formOf(a);
    return JSON.stringify({
      tag: a.tagName, type: (a.type || a.getAttribute("type") || "").toLowerCase(),
      role: roleOf(a), name: nameOf(a, roleOf(a), !!clickMarker(a)),
      method: form ? (form.getAttribute("method") || "get").toLowerCase() : "",
      action: form ? (form.getAttribute("action") || "") : "",
      btnMethod: a.getAttribute ? (a.getAttribute("formmethod") || "").toLowerCase() : "",
      btnAction: a.getAttribute ? (a.getAttribute("formaction") || "") : "",
      href: a.getAttribute ? (a.getAttribute("href") || "") : "",
    });
  }

  // §6.1：按 ref 返回结构信息，供工具层在执行 click/type 前判"有后果"。
  function info(opts) {
    const r = resolve(opts && opts.ref);
    if (!r.ok) return r;
    return { ok: true, ref: opts.ref, ...infoOf(r.el) };
  }

  // 结构信息的本体（info 按 ref 取，describeAt 按坐标取，共用一份口径）
  function infoOf(el) {
    const a = activatable(el);
    const role = roleOf(a);
    const form = formOf(a);
    return {
      docId, fp: fingerprint(el),
      selfTag: el.tagName.toLowerCase(),
      tag: a.tagName.toLowerCase(),
      // el.type 是 IDL 属性：没写 type 的 <button> 默认 "submit"、<input> 默认 "text"；
      // getAttribute 会读成空串，漏判表单默认提交按钮
      type: (a.type || a.getAttribute("type") || "").toLowerCase(),
      role, name: nameOf(a, role, !!clickMarker(a)),
      inForm: !!form,
      formMethod: form ? (form.getAttribute("method") || "get").toLowerCase() : "",
      // 没写 method：React / Vue 的「onSubmit + fetch POST」大多这样，结构上看不出是 GET 还是写操作
      formMethodSet: form ? form.hasAttribute("method") : false,
      submitName: form ? defaultSubmitName(form) : "",
      formAction: form ? (form.getAttribute("action") || "") : "",
      // 提交按钮自己的 formmethod / formaction 盖过表单的（复审 #20）
      btnFormMethod: (a.getAttribute("formmethod") || "").toLowerCase(),
      btnFormAction: a.getAttribute("formaction") || "",
    };
  }

  // 执行时核对批准时的文档与元素指纹：换文档 / 元素被改 → 拒绝，让模型重新快照
  function checkExpect(opts, el) {
    const expect = opts && opts.expect;
    if (!expect) return null;
    if (expect.doc && expect.doc !== docId)
      return { ok: false, error: "ref 已失效：页面已换文档，重新 browser_snapshot 拿新 ref" };
    if (expect.fp && expect.fp !== fingerprint(el))
      return { ok: false, error: "元素自批准后已被页面修改（结构指纹不一致），重新 browser_snapshot" };
    return null;
  }

  // ── 真实鼠标点击（复审 #9）──────────────────────────────────
  // 合成的 el.click() 只发一个 click：靠 mousedown / pointerdown 打开的组件（antd Select、
  // Radix 菜单）点不开，还报「样式都没有变化」；被全屏遮罩盖住的按钮也能点中（真人点不到）；
  // 打开的新窗口在 Qt 看来不算用户发起。改成工具层往 view 发真实鼠标事件，这里负责：
  // 滚到可见、算点击坐标、命中检测（点下去落在谁身上）、确认事件真的到了。
  const arrival = { el: null, down: false, click: false };

  function within(node, el) {
    for (let n = node; n; n = n.parentElement || (n.getRootNode && n.getRootNode().host) || null)
      if (n === el) return true;
    return false;
  }

  // 落点和目标在同一个小组件里（往上 3 层内有共同祖先，不算 body / html）：组件库常把真正的
  // 输入框做成透明的，上面叠一块显示当前值的兄弟元素（antd Select），真人点的就是那块。
  // 全屏遮罩和目标的共同祖先是 body，照样算挡住。
  function sameWidget(hit, el) {
    let a = el;
    for (let i = 0; i < 3; i++) {
      a = a.parentElement || (a.getRootNode && a.getRootNode().host) || null;
      if (!a || a === document.body || a === document.documentElement) return false;
      if (within(hit, a)) return true;
    }
    return false;
  }

  function hitAt(x, y) {
    let hit = document.elementFromPoint(x, y);
    while (hit && hit.shadowRoot) {          // 开放 shadow DOM：往里找到真正落点
      const inner = hit.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    return hit;
  }

  for (const t of ["pointerdown", "mousedown", "click"])
    window.addEventListener(t, e => {
      const el = arrival.el;
      if (!el || !e.isTrusted) return;
      if (within(e.target, el) || within(el, e.target) || sameWidget(e.target, el)) {
        if (t === "click") arrival.click = true; else arrival.down = true;
      }
    }, true);

  function pointOf(opts) {
    const r = resolve(opts && opts.ref);
    if (!r.ok) return r;
    const el = r.el;
    const bad = checkExpect(opts, el);
    if (bad) return bad;
    arrival.el = null;
    // 原生 <select> 的选项在系统弹窗里，鼠标点不到：照旧走脚本
    if (el.tagName === "OPTION") return { ok: true, synthetic: true, why: "option" };
    el.scrollIntoView({ block: "center", inline: "center" });
    const rect = el.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1)
      return { ok: true, synthetic: true, why: "no-box" };   // 透明 0 尺寸的原生控件等
    // 中心点被挡就试几个内部点（角上贴着别的元素、中间有个浮动图标……）
    const pts = [[0.5, 0.5], [0.25, 0.5], [0.75, 0.5], [0.5, 0.25], [0.5, 0.75]];
    let blocker = null, bx = 0, by = 0;
    for (const [fx, fy] of pts) {
      const x = rect.left + rect.width * fx, y = rect.top + rect.height * fy;
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
      const hit = hitAt(x, y);
      if (!hit) continue;
      if (within(hit, el) || within(el, hit) || sameWidget(hit, el)) {
        arrival.el = el; arrival.down = false; arrival.click = false;
        return { ok: true, x, y };
      }
      if (!blocker) { blocker = hit; bx = x; by = y; }
    }
    if (blocker)
      return { ok: false, occluded: true, error: "元素被别的东西挡住了", by: describe(blocker), x: bx, y: by };
    return { ok: true, synthetic: true, why: "offscreen" };
  }

  // 输入时「灵犀的鼠标」停在哪：输入框右边、垂直居中（页面 CSS 坐标，看不见就不给）
  function anchorOf(opts) {
    const r = resolve(opts && opts.ref);
    if (!r.ok) return r;
    const rect = r.el.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > innerHeight || rect.width < 1)
      return { ok: false, error: "不在可见区域" };
    return { ok: true, x: Math.min(rect.right - 6, innerWidth - 12), y: rect.top + rect.height / 2 };
  }

  function pointArrived() {
    return { ok: true, down: arrival.down, click: arrival.click };
  }

  // 按坐标看那一点上是什么（canvas 游戏 / 图表里的东西没有 ref，模型从截图拿坐标来点）。
  // 不滚动、不做遮挡判定——坐标点就是要点的地方，落在谁身上就点谁；登记送达目标，
  // 工具层发完真实点击后用 pointArrived 确认。落在 iframe 上时只报 iframe 的序号，
  // 由工具层换算进那个 frame 再来一次（跨域的里面这里看不到）。
  function describeAt(opts) {
    opts = opts || {};
    pruneRefs();
    if (opts.__seed) refSeq = Math.max(refSeq, opts.__seed);
    const x = Number(opts.x), y = Number(opts.y);
    if (!isFinite(x) || !isFinite(y) || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight)
      return { ok: false, error: "坐标不在可见区域里（本视口 " + innerWidth + "×" + innerHeight + "）" };
    const hit = hitAt(x, y);
    if (!hit) return { ok: false, error: "这一点上没有元素" };
    if (hit.tagName === "IFRAME" || hit.tagName === "FRAME") {
      const all = Array.from(document.querySelectorAll("iframe, frame"));
      return { ok: true, iframe: all.indexOf(hit), docId };
    }
    arrival.el = hit; arrival.down = false; arrival.click = false;
    const ref = assignRef(hit);
    const role = roleOf(hit);
    return { ok: true, x, y, ref, docId, nextSeq: refSeq,
             desc: describe(hit), info: infoOf(hit),
             interactive: INTERACTIVE.has(role) || !!clickMarker(hit) };
  }

  function click(opts) {
    const r = resolve(opts && opts.ref);
    if (!r.ok) return r;
    const el = r.el;
    const bad = checkExpect(opts, el);
    if (bad) return bad;
    // 点 option：合成 click 不改选中项，直接置 selected + 触发 change
    if (el.tagName === "OPTION") {
      const sel = el.closest("select");
      if (sel) {
        sel.value = el.value;
        el.selected = true;
        sel.dispatchEvent(new Event("input", { bubbles: true }));
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        return { ok: true, role: "option", name: nameOf(el, "option"), value: sel.value };
      }
    }
    el.scrollIntoView({ block: "center", inline: "center" });
    if (el.focus) el.focus();
    el.click();
    const role = roleOf(el);
    return { ok: true, role, name: nameOf(el, role) };
  }

  function type(opts) {
    opts = opts || {};
    const r = resolve(opts.ref);
    if (!r.ok) return r;
    const el = r.el;
    const bad = checkExpect(opts, el);
    if (bad) return bad;
    const text = opts.text || "";
    // select：原生 value setter 在 HTMLInputElement.prototype 上会 Illegal invocation，
    // 走匹配 option 置值 + change（复审 9）
    if (el.tagName === "SELECT") {
      const want = String(text);
      const opt = Array.from(el.options).find(
        o => o.value === want || (o.textContent || "").trim() === want);
      if (!opt) {
        // 选项文字是页面写的：单独给，由工具层围进 untrusted 围栏（复审 #19），不拼进错误文案
        return { ok: false, error: "select 没有匹配的选项",
          options: Array.from(el.options).slice(0, 50).map(
            o => clean(o.value || o.textContent || "", 80)) };
      }
      el.value = opt.value;
      opt.selected = true;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true, value: el.value };
    }
    // 只有 input / textarea / 可编辑区能收文字。role=textbox 的 div 拿 input 的 value setter
    // 调会抛「Illegal invocation」（复审 #11）：明说，让模型去找里面真正的输入框
    if (!el.isContentEditable && el.tagName !== "INPUT" && el.tagName !== "TEXTAREA")
      return { ok: false, error: "这个元素不能直接输入文字（不是输入框，也不是可编辑区）："
        + "先点它，再在快照里找里面真正的输入框" };
    el.scrollIntoView({ block: "center" });
    el.focus();
    if (el.isContentEditable) {
      if (opts.clear !== false) el.textContent = "";
      el.textContent += text;
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    } else {
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
      setter.call(el, opts.clear === false ? el.value + text : text);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
    if (opts.submit) {
      const form = el.form || el.closest("form");
      if (form && form.requestSubmit) form.requestSubmit();
      else pressEnter(el);
    }
    if (isSecret(el)) return { ok: true, secret: true, filled: !!el.value };
    return { ok: true, value: (el.value || el.textContent || "").slice(0, 100) };
  }

  // 没有表单的回车：按真实按键的顺序发 keydown → keypress → keyup。以前只发 keydown，
  // 监听 keyup 的（Vue @keyup.enter、很多搜索框）收不到（复审 #12）。老代码看 keyCode / which：
  // 放在构造参数里（Chromium 认这几个旧字段）。事后在实例上 defineProperty 不行——我们在隔离
  // 世界里，页面脚本拿到的是另一个包装对象，看不到隔离世界加的属性（实测 keyCode 仍是 0）
  function pressEnter(el) {
    for (const type of ["keydown", "keypress", "keyup"]) {
      el.dispatchEvent(new KeyboardEvent(type, {
        key: "Enter", code: "Enter", bubbles: true, cancelable: true,
        keyCode: 13, which: 13, charCode: type === "keypress" ? 13 : 0,
      }));
    }
  }

  function find(opts) {
    opts = opts || {};
    pruneRefs();
    if (opts.__seed) refSeq = Math.max(refSeq, opts.__seed);
    const text = opts.text || "";
    const limit = opts.limit || 20;
    const q = text.toLowerCase();
    const out = [];
    const stack = Array.from(document.body.children);
    let visited = 0;
    while (stack.length && out.length < limit && visited < WALK_NODE_CAP) {
      const el = stack.pop();
      visited++;
      if (SKIP_TAG.has(el.tagName) || !visible(el)) continue;
      if (el.shadowRoot) stack.push(...el.shadowRoot.children);
      stack.push(...el.children);
      const role = roleOf(el);
      if (role === "hidden" || role === "presentation") continue;
      const click = role === "generic" ? clickMarker(el) : "";
      if (role === "generic" && !click) continue;
      // 读屏孪生体不进 find（和快照同一口径）：点了没用，别让它排在可见选项前面
      if (isA11yTwin(el, role)) continue;
      const name = nameOf(el, role, !!click);
      const hay = (name + " " + (el.getAttribute("placeholder") || "")).toLowerCase();
      if (!hay.includes(q)) continue;
      const kids = el.shadowRoot ? Array.from(el.shadowRoot.children) : Array.from(el.children);
      if (kids.some(c => visible(c) && (roleOf(c) !== "generic" || clickMarker(c))
          && nameOf(c, roleOf(c), !!clickMarker(c)).toLowerCase().includes(q))) continue;
      const m = { ref: assignRef(el), role, name, tag: el.tagName.toLowerCase() };
      if (click) m.click = click;
      if (role === "link") {
        const href = el.getAttribute("href") || "";
        if (href) m.href = href.slice(0, 200);
      }
      out.push(m);
    }
    return { ok: true, query: text, matches: out, count: out.length, url: location.href, docId, nextSeq: refSeq };
  }

  function readText(opts) {
    opts = opts || {};
    const maxChars = opts.maxChars || 8000;
    // 多 article 页面不能只读第一篇（复审 11）：优先 main/[role=main]，
    // 没有 main 时合并所有 article，再退 body
    let parts, source;
    const main = document.querySelector("main, [role=main]");
    if (main) {
      parts = [main.innerText || ""];
      source = main.tagName.toLowerCase();
    } else {
      const arts = Array.from(document.querySelectorAll("article"));
      if (arts.length) {
        parts = arts.map(a => a.innerText || "");
        source = "article*" + arts.length;
      } else {
        parts = [document.body.innerText || ""];
        source = "body";
      }
    }
    let text = parts.join("\n\n").replace(/\n{3,}/g, "\n\n").trim();
    const truncated = text.length > maxChars;
    if (truncated) text = text.slice(0, maxChars);
    return { ok: true, chars: text.length, truncated, text, source, url: location.href };
  }

  function act(opts) {
    const steps = (opts && opts.steps) || [];
    const results = [];
    for (let i = 0; i < steps.length; i++) {
      const s = steps[i];
      let r;
      if (s.op === "click") r = click(s);
      else if (s.op === "type") r = type(s);
      else r = { ok: false, error: "未知 op: " + s.op };
      results.push({ op: s.op, ref: s.ref, ...r });
      if (!r.ok) return { ok: false, stoppedAt: i, results };
    }
    return { ok: true, results };
  }

  // ── 批量填表（browser_fill_form）──────────────────────────────────
  // 实测：模试页 130 个下拉框 + 193 个输入框，模型觉得逐个 browser_type 不现实，绕去找外部
  // Chrome 的调试端口，二十多轮一个空没填。fillInfo 一次取齐每个 ref 的结构信息和能填成什么，
  // 用户整批批准后 fill 一次填完；单个字段失败不影响其它，各自报原因。

  // 这个元素能怎么填："text" / "select" / "checkbox" / "radio"；不能填（按钮、链接……）返回 ""
  function fieldKind(el) {
    if (el.tagName === "SELECT") return "select";
    if (el.tagName === "TEXTAREA") return "text";
    if (el.tagName === "INPUT") {
      const t = (el.type || "text").toLowerCase();
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (["submit", "button", "reset", "image", "file", "hidden"].includes(t)) return "";
      return "text";
    }
    if (el.isContentEditable) return "text";
    const role = roleOf(el);
    // 自己做的勾选框（div[role=checkbox]）：点一下切换，状态看 aria-checked
    if (role === "checkbox" || role === "switch") return "checkbox";
    if (role === "radio") return "radio";
    return "";
  }

  function isChecked(el) {
    if (el.tagName === "INPUT") return !!el.checked;
    return (el.getAttribute("aria-checked") || "").toLowerCase() === "true";
  }

  function fillInfo(opts) {
    const out = [];
    for (const ref of (opts && opts.refs) || []) {
      const r = resolve(ref);
      if (!r.ok) { out.push({ ref, ok: false, error: r.error }); continue; }
      const kind = fieldKind(r.el);
      out.push({ ref, ok: true, kind, secret: isSecret(r.el),
                 disabled: !!(r.el.disabled || r.el.getAttribute("aria-disabled") === "true"),
                 ...infoOf(r.el) });
    }
    return { ok: true, docId, items: out };
  }

  function fill(opts) {
    const results = [];
    for (const f of (opts && opts.fields) || []) {
      const r = resolve(f.ref);
      if (!r.ok) { results.push({ ref: f.ref, ok: false, error: r.error }); continue; }
      const el = r.el;
      const bad = checkExpect(f, el);
      if (bad) { results.push({ ref: f.ref, ...bad }); continue; }
      const kind = fieldKind(el);
      if (kind === "text" || kind === "select") {
        const t = type({ ref: f.ref, text: String(f.value), clear: true });
        results.push({ ref: f.ref, kind, ...t });
        continue;
      }
      if (kind === "checkbox" || kind === "radio") {
        const want = !!f.value;
        if (isChecked(el) === want) {
          results.push({ ref: f.ref, kind, ok: true, checked: want, unchanged: true });
          continue;
        }
        if (kind === "radio" && !want) {
          results.push({ ref: f.ref, kind, ok: false,
                         error: "单选框不能直接取消：选同一组里的另一个" });
          continue;
        }
        el.scrollIntoView({ block: "center" });
        el.click();
        const now = isChecked(el);
        results.push(now === want
          ? { ref: f.ref, kind, ok: true, checked: now }
          : { ref: f.ref, kind, ok: false, checked: now, error: "点了但勾选状态没变（页面拦下了）" });
        continue;
      }
      results.push({ ref: f.ref, ok: false, error: "不是能填的字段" });
    }
    return { ok: true, docId, results };
  }

  // console 通道：隔离世界拦不到页面 console.*（那些走 Python javaScriptConsoleMessage）。
  // 这里只收资源加载失败（捕获 phase 的 error 事件，img/script/link/iframe）——
  // Qt 不暴露响应状态码，只记失败 URL（设计文档已知限制）。
  const consoleBuf = [];
  const push = (level, text) => {
    consoleBuf.push({ level, text: String(text).slice(0, 2000), t: Date.now() });
    if (consoleBuf.length > 200) consoleBuf.shift();
  };
  window.addEventListener("error", e => {
    const t = e.target;
    if (t && t !== window && t.tagName &&
        ["IMG", "SCRIPT", "LINK", "IFRAME"].includes(t.tagName)) {
      push("error", "资源加载失败: " + (t.src || t.href || "(unknown)"));
    } else if (e.message) {
      push("error", (e.message || "error") + " @ " + (e.filename || "") + ":" + (e.lineno || 0));
    }
  }, true);
  window.addEventListener("unhandledrejection", e =>
    push("error", "unhandledrejection: " + (e.reason && (e.reason.stack || e.reason.message) || e.reason)));

  // ── 动作前后的样式核对（文字树看不见的纯样式变化，tools_browser._delta_text 只在文字树
  // diff 为空时取）──────────────────────────────────────────────────────────────
  //
  // 关键是**不报假变化**：不看 class 名猜含义，而是动作前记下点击处附近元素的实际计算样式，
  // 动作后再取一次逐项比——class 变了但样式没变（埋点 class、焦点 class 没写样式）不会报。
  // 我们自己的副作用（滚动到可见、聚焦）在记基线之前做完，不会混进来。
  // 动作前空闲期就在自己变的元素（时钟、轮播、加载动画）单独标出，不当成动作的效果。
  const VISUAL_PROPS = ["display", "visibility", "opacity", "color", "backgroundColor",
    "backgroundImage", "textDecorationLine", "fontWeight", "fontStyle", "borderTopColor",
    "borderRightColor", "borderBottomColor", "borderLeftColor", "outlineStyle", "outlineColor",
    "boxShadow", "transform", "filter"];
  const PSEUDO_PROPS = ["content", "color", "backgroundColor", "display"];
  const CSS_NAME = p => p.replace(/[A-Z]/g, c => "-" + c.toLowerCase());
  const SCOPE_CAP = 120;
  const MUT_CAP = 4000;
  let mutObs = null;
  let mutated = new Set();      // 上次 takeRecords 以来属性变过的元素
  let busy = new WeakSet();     // 动作前空闲期就在自己变的元素
  let baseline = null;          // {target, items: [{el, st, pseudo}]}

  function noteRecords(records) {
    for (const m of records) {
      if (m.type !== "attributes") continue;
      if (mutated.size < MUT_CAP) mutated.add(m.target);
    }
  }

  function ensureObserver() {
    if (mutObs || !document.documentElement) return;
    mutObs = new MutationObserver(noteRecords);
    mutObs.observe(document.documentElement, { subtree: true, attributes: true });
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", ensureObserver, { once: true });
  else ensureObserver();

  function drainMutations() {
    if (mutObs) noteRecords(mutObs.takeRecords());
    const out = mutated;
    mutated = new Set();
    return out;
  }

  function rendered(el) {
    return el.isConnected && el.getClientRects().length > 0;
  }

  // 一直在跑的 CSS 动画（加载转圈等）：它动的属性随时在变，比较时跳过
  function loopingProps(el) {
    const skip = new Set();
    try {
      for (const a of el.getAnimations()) {
        const timing = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming() : null;
        if (!timing || timing.iterations !== Infinity) continue;
        const kf = a.effect.getKeyframes ? a.effect.getKeyframes() : [];
        for (const f of kf) for (const k of Object.keys(f)) skip.add(k);
      }
    } catch (e) { /* 拿不到就不跳过 */ }
    return skip;
  }

  function styleOf(el) {
    const cs = getComputedStyle(el);
    const st = {};
    for (const p of VISUAL_PROPS) st[p] = cs[p];
    const pseudo = {};
    for (const which of ["::before", "::after"]) {
      const ps = getComputedStyle(el, which);
      if (ps.content && ps.content !== "none" && ps.content !== "normal")
        for (const p of PSEUDO_PROPS) pseudo[which + " " + p] = ps[p];
    }
    return { st, pseudo };
  }

  // 点击处附近：目标往上两层的整棵子树（同一行 / 同一项里的兄弟元素，删除线常画在旁边的
  // 文字上）+ 祖先链 + aria-controls / label 指向的元素
  function scopeOf(target) {
    const set = new Set();
    let root = target;
    for (let i = 0; i < 2 && root.parentElement && root.parentElement !== document.body; i++)
      root = root.parentElement;
    // 祖先在前（自上而下）、子树按层序在后：报告时父元素先比，继承下来的同样变化才能去重
    const chain = [];
    for (let a = root.parentElement; a && a !== document.documentElement; a = a.parentElement)
      chain.unshift(a);
    for (const a of chain) set.add(a);
    const queue = [root];
    while (queue.length && set.size < SCOPE_CAP) {
      const el = queue.shift();
      if (SKIP_TAG.has(el.tagName)) continue;
      set.add(el);
      for (const c of el.children) queue.push(c);
    }
    const ids = (target.getAttribute("aria-controls") || "").split(/\s+/).filter(Boolean);
    if (target.control) set.add(target.control);
    for (const id of ids) { const t = document.getElementById(id); if (t) set.add(t); }
    if (!set.has(target)) set.add(target);
    return Array.from(set);
  }

  function watchPrepare(opts) {
    // 空闲期（上一个动作之后到现在）变过的元素：它们会自己变，不是这次动作的效果
    ensureObserver();
    busy = new WeakSet();
    for (const el of drainMutations()) busy.add(el);
    baseline = null;
    const r = resolve(opts && opts.ref);
    if (!r.ok) return r;
    // 先把我们自己的副作用做完（click/type 里还会再做一遍，届时已是空操作）
    r.el.scrollIntoView({ block: "center", inline: "center" });
    if (r.el.focus) r.el.focus();
    return { ok: true };
  }

  // 作用范围里正在跑的**有限时长**动画 / 过渡（复审 #1 #2）：聚焦样式带 transition
  // （bootstrap / antd 常见）、滚到可见触发的淡入，都是我们自己的准备动作带出来的，
  // 基线要等它们跑完再记，否则动作前后一比，过渡的后半段就被报成「动作的效果」。
  // 一直循环的（转圈）不在这里等，比较时按属性跳过（loopingProps）。
  function finiteRunning(scope) {
    const set = new Set(scope);
    const out = [];
    try {
      for (const a of document.getAnimations()) {
        const t = a.effect && a.effect.target;
        if (!t || !set.has(t) || a.playState !== "running") continue;
        const timing = a.effect.getComputedTiming();
        if (!isFinite(timing.endTime)) continue;
        const left = timing.endTime - (timing.localTime || 0);
        if (left > 0) out.push({ el: t, left });
      }
    } catch (e) { /* 拿不到就当没有 */ }
    return out;
  }

  function watchSettling(opts) {
    const r = resolve(opts && opts.ref);
    if (!r.ok) return r;
    const running = finiteRunning(scopeOf(r.el));
    return { ok: true, ms: Math.ceil(running.reduce((m, x) => Math.max(m, x.left), 0)) };
  }

  function watchBaseline(opts) {
    const r = resolve(opts && opts.ref);
    if (!r.ok) return r;
    drainMutations();                       // 聚焦、滚动带出来的属性变化：丢掉
    const scope = scopeOf(r.el);
    // 等过了上限还在过渡的：动作前就在变，不算动作的效果
    let still = 0;
    for (const x of finiteRunning(scope)) { busy.add(x.el); still++; }
    const items = scope.map(el => ({ el, ...styleOf(el) }));
    baseline = { target: r.el, items, docId };
    return { ok: true, scope: items.length, still };
  }

  function describe(el) {
    const role = roleOf(el);
    let name = nameOf(el, role);
    if (!name) name = clean(el.textContent, 40);
    let d = role === "generic" ? el.tagName.toLowerCase() : role;
    if (name) d += " " + JSON.stringify(name);
    const ref = elRefs.get(el);
    if (ref && refs.get(ref) === el) d += " [ref=" + ref + "]";
    return d;
  }

  function watchReport() {
    if (!baseline || baseline.docId !== docId)
      return { ok: false, error: "没有动作前的样式基线" };
    const after = drainMutations();
    const changed = [];
    let busyCount = 0;
    const seen = new Map();                 // el -> 变化签名，用来去掉继承下来的重复
    for (const it of baseline.items) {
      const el = it.el;
      if (!el.isConnected) continue;
      const now = styleOf(el);
      const loop = loopingProps(el);
      const diffs = [];
      for (const p of VISUAL_PROPS) {
        if (loop.has(p)) continue;
        if (it.st[p] === now.st[p]) continue;
        // 边框 / 轮廓色默认跟着 currentColor：文字变色时它们一起变，是同一件事，不重复报
        if (/^(border|outline).*Color$/.test(p) && it.st[p] === it.st.color && now.st[p] === now.st.color)
          continue;
        diffs.push([CSS_NAME(p), it.st[p], now.st[p]]);
      }
      const keys = new Set([...Object.keys(it.pseudo), ...Object.keys(now.pseudo)]);
      for (const k of keys) {
        if ((it.pseudo[k] || "") !== (now.pseudo[k] || ""))
          diffs.push([k.replace(/ ([a-zA-Z]+)$/, (m, p) => " " + CSS_NAME(p)), it.pseudo[k] || "(无)", now.pseudo[k] || "(无)"]);
      }
      if (!diffs.length) continue;
      // 现在看不见的元素：只有"被隐藏"本身算变化
      if (!rendered(el) && !diffs.some(d => d[0] === "display" || d[0] === "visibility")) continue;
      const sig = JSON.stringify(diffs);
      seen.set(el, sig);
      if (el.parentElement && seen.get(el.parentElement) === sig) continue;  // 继承自父元素
      if (busy.has(el)) { busyCount++; continue; }
      changed.push({
        desc: describe(el),
        self: el === baseline.target,
        props: diffs.slice(0, 4).map(d => ({ prop: d[0], from: String(d[1]).slice(0, 60), to: String(d[2]).slice(0, 60) })),
      });
    }
    const scoped = new Set(baseline.items.map(i => i.el));
    let elsewhere = 0;
    for (const el of after) if (!scoped.has(el) && !busy.has(el) && rendered(el)) elsewhere++;
    baseline = null;
    return { ok: true, changed: changed.slice(0, 6), more: Math.max(0, changed.length - 6),
             busy: busyCount, elsewhere };
  }

  // ── iframe 换算 / 按键 / 拖拽 / 局部截图 ──────────────────────
  // 本文档里各个 iframe 内容区（去掉边框和内边距）在本视口里的位置：iframe 里的坐标加上它
  // 才是外层的坐标。工具层按 Qt 那边的 frame 尺寸把 iframe 对上号（跨域的 iframe 里拿不到
  // frameElement，只能从外层量）
  function frameBoxes() {
    const out = [];
    document.querySelectorAll("iframe, frame").forEach((f, index) => {
      const r = f.getBoundingClientRect();
      const cs = getComputedStyle(f);
      const pl = parseFloat(cs.paddingLeft) || 0, pt = parseFloat(cs.paddingTop) || 0;
      const pr = parseFloat(cs.paddingRight) || 0, pb = parseFloat(cs.paddingBottom) || 0;
      out.push({ index, src: f.src || "", x: r.left + f.clientLeft + pl, y: r.top + f.clientTop + pt,
                 w: f.clientWidth - pl - pr, h: f.clientHeight - pt - pb });
    });
    return { ok: true, frames: out, vw: innerWidth, vh: innerHeight };
  }

  function revealFrame(opts) {
    const f = document.querySelectorAll("iframe, frame")[opts && opts.index];
    if (!f) return { ok: false, error: "iframe 不在了" };
    f.scrollIntoView({ block: "nearest", inline: "nearest" });
    return { ok: true };
  }

  // 元素在本视口里的矩形（不滚动；拖拽的落点、局部截图用）
  function rectOf(opts) {
    const r = resolve(opts && opts.ref);
    if (!r.ok) return r;
    const b = r.el.getBoundingClientRect();
    return { ok: true, x: b.left, y: b.top, w: b.width, h: b.height, vw: innerWidth, vh: innerHeight };
  }

  // 按键前把焦点交给这个元素 / 这个 frame（键盘事件送给有焦点的地方）
  function focusRef(opts) {
    const r = resolve(opts && opts.ref);
    if (!r.ok) return r;
    const bad = checkExpect(opts, r.el);
    if (bad) return bad;
    r.el.scrollIntoView({ block: "center", inline: "center" });
    if (r.el.focus) r.el.focus();
    return { ok: true };
  }

  function focusWindow() {
    window.focus();
    return { ok: true };
  }

  // 按键真的送到了这个文档没有（真实键盘事件 isTrusted）
  const keyArrival = { key: null };
  window.addEventListener("keydown", e => { if (e.isTrusted) keyArrival.key = e.key; }, true);
  function keyWatch() { keyArrival.key = null; return { ok: true }; }
  function keyArrived() { return { ok: true, key: keyArrival.key }; }

  // 截图前把 ref 滚到可见（以前主世界按 data-lxref 属性查，现在 ref 不进 DOM）
  function reveal(opts) {
    const r = resolve(opts && opts.ref);
    if (!r.ok) return r;
    r.el.scrollIntoView({ block: "center", inline: "center" });
    return { ok: true };
  }

  // ── 滚动（真实滚轮事件的落点 / 位置读数；wheel 本身由工具层发给 view）──────────
  // 给 ref 滚那个可滚动容器：ref 本身不能滚就往上找最近的可滚动祖先；不给滚页面。
  function scrollableAncestor(el) {
    for (let n = el; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (/(auto|scroll|overlay)/.test(cs.overflowY + " " + cs.overflowX)
          && (n.scrollHeight > n.clientHeight + 1 || n.scrollWidth > n.clientWidth + 1)) return n;
    }
    return document.scrollingElement || document.documentElement;
  }

  function scrollTarget(opts) {
    if (opts && opts.ref) {
      const r = resolve(opts.ref);
      if (!r.ok) return r;
      return { ok: true, el: scrollableAncestor(r.el) };
    }
    return { ok: true, el: document.scrollingElement || document.documentElement };
  }

  function scrollMetrics(el) {
    return { x: el.scrollLeft, y: el.scrollTop,
             maxX: Math.max(0, el.scrollWidth - el.clientWidth),
             maxY: Math.max(0, el.scrollHeight - el.clientHeight) };
  }

  // 滚动前：把容器滚进视野（我们自己的副作用，要在动作前快照之前做完），给出滚轮落点
  // （容器可见部分的中心，页面 CSS 坐标）和当前位置。
  function scrollInfo(opts) {
    const t = scrollTarget(opts);
    if (!t.ok) return t;
    const el = t.el;
    const doc = el === document.scrollingElement || el === document.documentElement || el === document.body;
    if (!doc) el.scrollIntoView({ block: "nearest", inline: "nearest" });
    const r = doc ? { left: 0, top: 0, right: innerWidth, bottom: innerHeight }
                  : el.getBoundingClientRect();
    const l = Math.max(r.left, 0), tp = Math.max(r.top, 0);
    const rr = Math.min(r.right, innerWidth), bb = Math.min(r.bottom, innerHeight);
    if (rr - l < 4 || bb - tp < 4) return { ok: false, error: "要滚的容器不在可见区域里" };
    // 落点要落在「滚的就是目标」的地方：中心可能压着嵌套的可滚动容器（页面里的盒子、
    // 弹层），wheel 会滚那个容器而不是目标——试几个候选点，命中检测确认
    // 先试中间和几个常见位置，再按 5×5 网格扫一遍；都压在别的滚动区域上就报 clean=false，
    // 工具层改用直接设位置（滚轮落到别的容器上会滚错地方，或被那个容器拦掉）
    let px = (l + rr) / 2, py = (tp + bb) / 2, clean = false;
    const tries = [[.5, .5], [.5, .8], [.5, .2], [.8, .5], [.2, .5], [.8, .8], [.2, .2]];
    for (let i = 0; i < 5; i++) for (let j = 0; j < 5; j++) tries.push([.1 + .2 * i, .1 + .2 * j]);
    for (const [fx, fy] of tries) {
      const x = l + (rr - l) * fx, y = tp + (bb - tp) * fy;
      const hit = hitAt(x, y);
      if (hit && scrollableAncestor(hit) === el) { px = x; py = y; clean = true; break; }
    }
    return { ok: true, doc, clean, ...scrollMetrics(el), px, py,
             cw: doc ? innerWidth : el.clientWidth, ch: doc ? innerHeight : el.clientHeight,
             vw: innerWidth, vh: innerHeight };
  }

  // 滚动后核对位置真的变了没有（到底了 / 被页面拦了都要如实说）
  function scrollState(opts) {
    const t = scrollTarget(opts);
    if (!t.ok) return t;
    return { ok: true, ...scrollMetrics(t.el) };
  }

  // 滚轮落不到目标上时的退路：直接按像素改位置（滚动加载靠 wheel 的页面不会触发加载）
  function scrollByPx(opts) {
    const t = scrollTarget(opts);
    if (!t.ok) return t;
    t.el.scrollLeft += Number(opts.dx) || 0;
    t.el.scrollTop += Number(opts.dy) || 0;
    return { ok: true, ...scrollMetrics(t.el) };
  }

  // top / bottom：直接设滚动位置（规范允许；位置照样核对）
  function scrollSet(opts) {
    const t = scrollTarget(opts);
    if (!t.ok) return t;
    const el = t.el;
    if (opts && opts.dir === "top") el.scrollTop = 0;
    else el.scrollTop = el.scrollHeight;
    return { ok: true, ...scrollMetrics(el) };
  }

  // ── 悬停送达确认：真实 MouseMove 到了目标没有（isTrusted 的 pointerover / mouseover）──
  const hoverArrival = { el: null, over: false };
  for (const t of ["pointerover", "mouseover", "pointerenter"])
    window.addEventListener(t, e => {
      const el = hoverArrival.el;
      if (!el || !e.isTrusted) return;
      if (within(e.target, el) || within(el, e.target)) hoverArrival.over = true;
    }, true);
  function hoverWatch(opts) {
    const r = resolve(opts && opts.ref);
    if (!r.ok) return r;
    hoverArrival.el = r.el; hoverArrival.over = false;
    return { ok: true };
  }
  function hoverArrived() { return { ok: true, over: hoverArrival.over }; }

  // 元素还在不在页面上（等遮罩 / 弹层消失用）：脱离 DOM 或不可见（display:none 等）都算不在
  function hasRef(opts) {
    const r = resolve(opts && opts.ref);
    return { ok: true, present: !!(r.ok && visible(r.el)) };
  }

  window.__lx = {
    snapshot, find, click, type, act, readText, info, watchPrepare, watchSettling, watchBaseline,
    watchReport,
    reveal, pointOf, pointArrived, anchorOf, describeAt,
    frameBoxes, revealFrame, rectOf, focusRef, focusWindow, keyWatch, keyArrived,
    scrollInfo, scrollState, scrollSet, scrollByPx, hoverWatch, hoverArrived, hasRef,
    fillInfo, fill,
    console: (opts) => { const out = consoleBuf.slice(); if (opts && opts.clear) consoleBuf.length = 0; return { ok: true, entries: out, url: location.href, docId }; },
    state: () => ({ ok: true, url: location.href, title: document.title, readyState: document.readyState, generation, docId }),
  };
  return "installed";
})()