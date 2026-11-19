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

  function nameOf(el, role) {
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
      if (el.labels && el.labels.length)
        return clean(Array.from(el.labels).map(l => l.textContent).join(" "), NAME_MAX);
      const ph = el.getAttribute("placeholder");
      if (ph) return clean(ph, NAME_MAX);
      const t = (el.type || "").toLowerCase();
      if (tag === "INPUT" && (t === "submit" || t === "button" || t === "reset"))
        return clean(el.value || t, NAME_MAX);
    }
    if (tag === "IMG") return clean(el.getAttribute("alt") || "", NAME_MAX);
    if (!FROM_CONTENT.has(role)) return clean(el.title || "", NAME_MAX);
    const text = clean(el.textContent, NAME_MAX);
    if (text) return text;
    if (el.title) return clean(el.title, NAME_MAX);
    const img = el.querySelector && el.querySelector("img[alt]");
    if (img) return clean(img.getAttribute("alt"), NAME_MAX);
    return "";
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

  function lineFor(el, role, name, depth, withRef) {
    let line = "  ".repeat(depth) + "- " + role;
    if (name) line += " " + JSON.stringify(name);
    if (/^H[1-6]$/.test(el.tagName)) line += " [level=" + el.tagName[1] + "]";
    if (withRef) line += " [ref=" + assignRef(el) + "]";
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
        const withRef = isInteractive || LANDMARK.has(role) || role === "heading";
        if (mode === "interactive") {
          if (isInteractive || LANDMARK.has(role) || role === "heading" || role === "img")
            selfLine = lineFor(el, role, nameOf(el, role), depth, withRef);
        } else {
          if (role !== "generic") selfLine = lineFor(el, role, nameOf(el, role), depth, withRef);
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
            && !isInteractive && role !== "heading" && role !== "img") {
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

  // 结构指纹：批准（info）与执行（click/type）之间元素被页面改过就拒绝
  function fingerprint(el) {
    const a = activatable(el);
    const form = formOf(a);
    return JSON.stringify({
      tag: a.tagName, type: (a.type || a.getAttribute("type") || "").toLowerCase(),
      role: roleOf(a), name: nameOf(a, roleOf(a)),
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
    const el = r.el;
    const a = activatable(el);
    const role = roleOf(a);
    const form = formOf(a);
    return {
      ok: true, ref: opts.ref, docId, fp: fingerprint(el),
      selfTag: el.tagName.toLowerCase(),
      tag: a.tagName.toLowerCase(),
      // el.type 是 IDL 属性：没写 type 的 <button> 默认 "submit"、<input> 默认 "text"；
      // getAttribute 会读成空串，漏判表单默认提交按钮
      type: (a.type || a.getAttribute("type") || "").toLowerCase(),
      role, name: nameOf(a, role),
      inForm: !!form,
      formMethod: form ? (form.getAttribute("method") || "get").toLowerCase() : "",
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
    let blocker = null;
    for (const [fx, fy] of pts) {
      const x = rect.left + rect.width * fx, y = rect.top + rect.height * fy;
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
      const hit = hitAt(x, y);
      if (!hit) continue;
      if (within(hit, el) || within(el, hit) || sameWidget(hit, el)) {
        arrival.el = el; arrival.down = false; arrival.click = false;
        return { ok: true, x, y };
      }
      blocker = blocker || hit;
    }
    if (blocker) return { ok: false, occluded: true, error: "元素被别的东西挡住了", by: describe(blocker) };
    return { ok: true, synthetic: true, why: "offscreen" };
  }

  function pointArrived() {
    return { ok: true, down: arrival.down, click: arrival.click };
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
      else el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    }
    if (isSecret(el)) return { ok: true, secret: true, filled: !!el.value };
    return { ok: true, value: (el.value || el.textContent || "").slice(0, 100) };
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
      if (role === "generic" || role === "hidden" || role === "presentation") continue;
      const name = nameOf(el, role);
      const hay = (name + " " + (el.getAttribute("placeholder") || "")).toLowerCase();
      if (!hay.includes(q)) continue;
      const kids = el.shadowRoot ? Array.from(el.shadowRoot.children) : Array.from(el.children);
      if (kids.some(c => visible(c) && roleOf(c) !== "generic"
          && nameOf(c, roleOf(c)).toLowerCase().includes(q))) continue;
      const m = { ref: assignRef(el), role, name, tag: el.tagName.toLowerCase() };
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

  // 截图前把 ref 滚到可见（以前主世界按 data-lxref 属性查，现在 ref 不进 DOM）
  function reveal(opts) {
    const r = resolve(opts && opts.ref);
    if (!r.ok) return r;
    r.el.scrollIntoView({ block: "center", inline: "center" });
    return { ok: true };
  }

  window.__lx = {
    snapshot, find, click, type, act, readText, info, watchPrepare, watchSettling, watchBaseline,
    watchReport,
    reveal, pointOf, pointArrived,
    console: (opts) => { const out = consoleBuf.slice(); if (opts && opts.clear) consoleBuf.length = 0; return { ok: true, entries: out, url: location.href, docId }; },
    state: () => ({ ok: true, url: location.href, title: document.title, readyState: document.readyState, generation, docId }),
  };
  return "installed";
})()