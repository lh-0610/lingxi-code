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
    if (el.isContentEditable) return "textbox";
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
    return cs.display !== "none" && cs.visibility !== "hidden" && cs.visibility !== "collapse";
  }

  function pruneRefs() {
    // 只剔除已脱离页面的元素；活着的元素保留原 ref（root_ref/diff/find 快照不得改指向）
    for (const [id, el] of Array.from(refs)) {
      if (!el.isConnected) {
        refs.delete(id);
        elRefs.delete(el);
        if (el.removeAttribute) el.removeAttribute("data-lxref");
      }
    }
  }

  function assignRef(el) {
    const old = elRefs.get(el);
    if (old && refs.get(old) === el) return old;
    const id = "e" + (++refSeq);
    refs.set(id, el);
    elRefs.set(el, id);
    el.setAttribute("data-lxref", id);
    return id;
  }

  function lineFor(el, role, name, depth, withRef) {
    let line = "  ".repeat(depth) + "- " + role;
    if (name) line += " " + JSON.stringify(name);
    if (/^H[1-6]$/.test(el.tagName)) line += " [level=" + el.tagName[1] + "]";
    if (withRef) line += " [ref=" + assignRef(el) + "]";
    if (el.disabled) line += " [disabled]";
    if (role === "checkbox" || role === "radio" || role === "switch" || role === "menuitemcheckbox")
      line += " [" + (el.checked ? "checked" : "unchecked") + "]";
    const exp = el.getAttribute("aria-expanded");
    if (exp !== null) line += " [expanded=" + exp + "]";
    if (role === "textbox" || role === "searchbox" || role === "combobox") {
      const v = el.value || "";
      if (v) line += " [value=" + JSON.stringify(clean(v, 50)) + "]";
    }
    if (role === "option" && el.selected) line += " [selected]";
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
        return { ok: false, error: "select 没有匹配选项: " + want + "（可选: "
          + Array.from(el.options).map(o => o.value || (o.textContent || "").trim()).join("/") + "）" };
      }
      el.value = opt.value;
      opt.selected = true;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true, value: el.value };
    }
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

  window.__lx = {
    snapshot, find, click, type, act, readText, info,
    console: (opts) => { const out = consoleBuf.slice(); if (opts && opts.clear) consoleBuf.length = 0; return { ok: true, entries: out, url: location.href, docId }; },
    state: () => ({ ok: true, url: location.href, title: document.title, readyState: document.readyState, generation, docId }),
  };
  return "installed";
})()