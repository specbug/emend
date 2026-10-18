({width, height, margin, maxPages}) => {
    const body = document.body, root = document.documentElement;
    if (!body) return {error: "No XHTML body is available for pagination."};
    const original = getComputedStyle(body);
    const rootStyle = getComputedStyle(root);
    if (original.writingMode !== "horizontal-tb" || original.direction !== "ltr" ||
        rootStyle.writingMode !== "horizontal-tb" || rootStyle.direction !== "ltr") {
        return {error: "Paginated preview supports horizontal left-to-right document flow only; use --layout scroll."};
    }
    const elements = [...body.querySelectorAll("*")];
    if (elements.length > 20000) {
        return {error: "Paginated geometry exceeds the 20,000-element limit; use a smaller document or scrolling review."};
    }
    const observations = [];
    const positioned = elements.filter(e => ["fixed", "absolute"].includes(getComputedStyle(e).position));
    if (positioned.length) {
        observations.push(`Absolutely or fixed-positioned elements: ${positioned.length}; they may not follow page flow.`);
    }
    const spanners = elements.filter(e => getComputedStyle(e).columnSpan === "all");
    if (spanners.length) {
        return {error: "Column-spanning content is unsupported in paginated preview; use --layout scroll."};
    }
    // CSS page breaks are not column breaks in Chromium. Adapt only the preview DOM.
    let adapted = 0;
    for (const element of elements) {
        const style = getComputedStyle(element);
        for (const [camel, css] of [["breakBefore", "break-before"], ["breakAfter", "break-after"]]) {
            if (["page", "always", "left", "right", "recto", "verso"].includes(style[camel])) {
                element.style.setProperty(css, "column", "important");
                adapted++;
            }
        }
    }
    if (adapted) observations.push(`Page-break rules adapted to column breaks in this preview: ${adapted}. Left/right blank-page parity is not emulated.`);
    const apply = (element, rules) => {
        for (const [name, value] of Object.entries(rules)) element.style.setProperty(name, value, "important");
    };
    const common = {
        "box-sizing": "border-box", "margin": "0px", "border": "0px", "position": "static",
        "transform": "none", "float": "none", "display": "block", "min-width": "0px",
        "max-width": "none", "min-height": "0px", "max-height": "none", "overflow": "visible",
        "scroll-behavior": "auto", "scroll-snap-type": "none", "scroll-padding": "0px",
        "width": `${width}px`, "height": `${height}px`
    };
    apply(root, {...common, "padding": "0px", "column-count": "auto", "column-width": "auto"});
    apply(body, {...common, "padding": `${margin}px`, "column-count": "auto",
        "column-width": `${width - 2 * margin}px`, "column-gap": `${2 * margin}px`,
        "column-fill": "auto", "column-rule": "none"});
    window.scrollTo(0, 0);
    // Each column starts at margin + n * width. A full-width canvas makes the last
    // offset reachable even when its final line ends before the right page edge.
    const extent = Math.max(body.scrollWidth, root.scrollWidth);
    const pages = Math.max(1, Math.ceil((extent - 1) / width));
    if (!Number.isFinite(pages) || pages > maxPages) {
        return {error: `Paginated document exceeds the ${maxPages}-page limit.`};
    }
    apply(root, {"width": `${pages * width}px`});
    const contentWidth = width - margin * 2, contentHeight = height - margin * 2;
    let oversized = 0;
    for (const element of elements) {
        if (!element.matches("img,svg,table,pre,video,canvas")) continue;
        const rects = [...element.getClientRects()];
        if (rects.some(r => r.width > contentWidth + 2 || r.height > contentHeight + 2)) oversized++;
    }
    if (oversized) observations.push(`Images, tables, or other content exceed the page content box: ${oversized}; inspect cropping or spillover.`);
    // Fragment rectangles identify unbreakable words spilling into the inter-page
    // margin. Limit the work independently of the number of paragraphs/pages.
    let checked = 0, crossing = 0, truncated = false;
    textScan: for (const element of [body, ...elements]) {
        const style = getComputedStyle(element);
        if (style.visibility === "hidden" || style.display === "none") continue;
        for (const node of element.childNodes) {
            if (node.nodeType !== Node.TEXT_NODE || !node.textContent.trim()) continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            for (const rect of range.getClientRects()) {
                if (++checked > 50000) { truncated = true; break textScan; }
                if (!rect.width || !rect.height) continue;
                const localLeft = rect.left - Math.floor(rect.left / width) * width;
                if (localLeft < margin - 2 || localLeft + rect.width > width - margin + 2) crossing++;
            }
        }
    }
    if (crossing) observations.push(`Text fragments cross horizontal page margins: ${crossing}; inspect long words, indentation, or positioned text.`);
    if (truncated) observations.push("Text-fragment inspection stopped at 50,000 rectangles; remaining content is unmeasured.");
    const actualHeight = Math.max(root.scrollHeight, body.scrollHeight);
    if (actualHeight > height + 2) observations.push(`Vertical overflow remains after pagination: ${actualHeight} CSS px in a ${height} px page.`);
    return {page_count: pages, observations, document_height: actualHeight};
}
