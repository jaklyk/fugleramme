// Tap-to-listen (Settings.tap_to_listen): invisible targets over the birds on the
// kiosk's picture, and the card a tap on one opens. Loaded by kiosk.html only when
// /page.json answers, so a frame with it off serves the page it always did.

// Where the picture actually is inside its element. The img keeps its aspect with
// object-fit: contain, so a box wider or taller than the picture letterboxes it.
export function contentBox(element, natural) {
  const scale = Math.min(element.width / natural.width, element.height / natural.height);
  const width = natural.width * scale;
  const height = natural.height * scale;
  return {
    left: element.left + (element.width - width) / 2,
    top: element.top + (element.height - height) / 2,
    width,
    height,
  };
}

// A bird's box on the page (page pixels) as a target on screen (CSS pixels).
export function target(box, page, shown) {
  const [x0, y0, x1, y1] = box;
  const sx = shown.width / page.width;
  const sy = shown.height / page.height;
  return { left: shown.left + x0 * sx, top: shown.top + y0 * sy, width: (x1 - x0) * sx, height: (y1 - y0) * sy };
}

// Smallest first, so a small bird drawn over a big one's empty corner is the one a
// tap there finds: later targets sit on top.
export function stacking(birds) {
  const area = ({ box: [x0, y0, x1, y1] }) => (x1 - x0) * (y1 - y0);
  return [...birds].sort((a, b) => area(b) - area(a));
}

const ago = (iso) => {
  const seconds = (new Date(iso) - Date.now()) / 1000;
  const units = [["day", 86400], ["hour", 3600], ["minute", 60]];
  const words = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) return words.format(Math.round(seconds / size), unit);
  }
  return words.format(0, "minute");
};

const STYLE = `
  #birds { position: fixed; inset: 0; pointer-events: none; }
  #birds button { position: fixed; padding: 0; border: 0; background: none; pointer-events: auto; cursor: pointer; -webkit-tap-highlight-color: transparent; }
  #card { position: fixed; z-index: 2; width: min(22rem, calc(100vw - 32px)); max-height: calc(100vh - 32px); overflow: auto; box-sizing: border-box;
    padding: 1rem 1.1rem 0.9rem; background: #f2ede2 url(/paper.png); border: 1px solid #cfc7b6; border-radius: 6px;
    color: #3a3833; font: 0.95rem/1.4 Georgia, "Times New Roman", serif; }
  #card[hidden] { display: none; }
  #card .plate { display: block; max-width: 100%; max-height: 9rem; margin: 0 auto 0.6rem; }
  #card h2 { margin: 0; font: italic 1.35rem/1.2 Georgia, serif; }
  #card .sci { margin: 0; font-style: italic; color: #6b675e; }
  #card .when { margin: 0.4rem 0 0.7rem; color: #6b675e; font-size: 0.85rem; }
  #card .buttons { display: flex; gap: 0.5rem; }
  #card .buttons button { flex: 1; min-height: 44px; border: 1px solid #a89f8c; border-radius: 4px; background: transparent; color: inherit; font: inherit; }
  #card .buttons button[aria-pressed="true"] { background: #e4dccb; }
  #card .buttons button:disabled { opacity: 0.5; }
  #card .sono { display: block; width: 100%; margin-top: 0.7rem; border-radius: 3px; }
  #card .note, #card .credit { margin: 0.5rem 0 0; font-size: 0.8rem; color: #6b675e; }
  #card .credit a { color: inherit; }
  #card .close { position: absolute; top: 0.2rem; right: 0.3rem; width: 44px; height: 44px; border: 0; background: none; color: #6b675e; font-size: 1.4rem; }
`;

export function start(collage) {
  const style = document.createElement("style");
  style.textContent = STYLE;
  document.head.append(style);
  const layer = Object.assign(document.createElement("div"), { id: "birds" });
  const card = Object.assign(document.createElement("div"), { id: "card", hidden: true });
  card.setAttribute("role", "dialog");
  document.body.append(layer, card);
  // One player for both buttons, so a clip and a reference never play together.
  const player = new Audio();
  let page = null;
  let open = null; // the bird the card is showing
  let retry;

  const shownToken = () => new URL(collage.currentSrc || collage.src, location.href).searchParams.get("v");

  const build = () => {
    layer.replaceChildren();
    if (!page || collage.hidden || !collage.naturalWidth) return;
    const shown = contentBox(collage.getBoundingClientRect(), { width: collage.naturalWidth, height: collage.naturalHeight });
    for (const bird of stacking(page.birds)) {
      const spot = target(bird.box, page, shown);
      const button = document.createElement("button");
      button.setAttribute("aria-label", bird.common);
      Object.assign(button.style, { left: spot.left + "px", top: spot.top + "px", width: spot.width + "px", height: spot.height + "px" });
      // Kept from the kiosk's own tap handler, which would wake the controls.
      button.addEventListener("pointerdown", (e) => e.stopPropagation());
      button.addEventListener("click", () => show(bird, spot));
      layer.append(button);
    }
  };

  // Only the description of the picture on screen: a newer page's boxes wait for its picture.
  const refresh = async () => {
    try {
      const answer = await fetch("/page.json", { cache: "no-store" });
      if (!answer.ok) return;
      const next = await answer.json();
      const token = shownToken();
      if (token && next.token !== token) return;
      page = next;
      build();
    } catch (e) {} // the next picture tries again
  };

  const stop = () => {
    clearTimeout(retry);
    player.pause();
    player.removeAttribute("src");
    player.load();
    for (const b of card.querySelectorAll(".buttons button")) b.setAttribute("aria-pressed", "false");
  };

  const close = () => {
    stop();
    card.hidden = true;
    open = null;
  };

  const element = (tag, props, ...children) => {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...children);
    return node;
  };

  async function show(bird, spot) {
    stop();
    open = bird;
    const name = encodeURIComponent(bird.scientific);
    const sono = element("img", { className: "sono", alt: "", hidden: true });
    const note = element("p", { className: "note", hidden: true });
    const credit = element("p", { className: "credit", hidden: true });
    const heard = element("button", { type: "button", textContent: "Heard here" });
    const reference = element("button", { type: "button", textContent: "Reference", disabled: true });
    const buttons = element("div", { className: "buttons" }, heard, reference);
    const when = bird.detection ? "Last heard " + ago(bird.detection.at) : "";
    card.replaceChildren(
      element("button", { type: "button", className: "close", textContent: "×", ariaLabel: "Close", onclick: close }),
      element("img", { className: "plate", alt: "", src: "/listen/plate?name=" + name, onload: () => open === bird && place(spot) }),
      element("h2", { textContent: bird.common }),
      element("p", { className: "sci", textContent: bird.scientific }),
      element("p", { className: "when", textContent: when }),
      buttons,
      sono,
      note,
      credit,
    );
    card.hidden = false; // shown first: placing it reads its height
    place(spot);

    const play = (button, src, picture) => {
      stop();
      button.setAttribute("aria-pressed", "true");
      sono.src = picture;
      sono.hidden = !picture;
      player.src = src;
      player.play().catch(() => {});
    };

    const clip = bird.detection && bird.detection.id ? bird.detection.id : null;
    heard.hidden = !clip;
    const audio = "/listen/clip?what=audio&id=" + clip;
    const listenHere = async () => {
      // A one-byte ask first: a 503 means BirdNET-Go is still writing the clip.
      const probe = await fetch(audio, { headers: { Range: "bytes=0-0" }, cache: "no-store" }).catch(() => null);
      if (open !== bird) return;
      if (probe && probe.status === 404) { heard.hidden = true; return; }
      if (!probe || probe.status === 503) {
        const wait = Number(probe && probe.headers.get("Retry-After")) || 5;
        note.textContent = probe ? `The recording is still being saved. Trying again in ${wait} s.` : "BirdNET-Go is not answering.";
        note.hidden = false;
        if (probe) retry = setTimeout(listenHere, wait * 1000);
        return;
      }
      note.hidden = true;
      credit.hidden = true;
      play(heard, audio, "/listen/clip?what=spectrogram&id=" + clip);
    };
    heard.onclick = listenHere;

    // Fetched as the card opens: the first ask for a species downloads it on the frame.
    let found = null;
    reference.textContent = "Reference…";
    fetch("/listen/reference?name=" + name)
      .then((answer) => (answer.ok ? answer.json() : null))
      .catch(() => null)
      .then((answer) => {
        if (open !== bird) return;
        found = answer;
        reference.textContent = "Reference";
        reference.disabled = !found;
        reference.hidden = !found;
      });
    reference.onclick = () => {
      if (!found) return;
      note.hidden = true;
      const by = `${found.kind ? found.kind[0].toUpperCase() + found.kind.slice(1) : "Recording"} by ${found.recordist}${found.country ? ", " + found.country : ""}. `;
      credit.replaceChildren(
        by,
        element("a", { href: found.link, target: "_blank", rel: "noopener", textContent: "XC" + found.id }),
        " · ",
        element("a", { href: found.licence, target: "_blank", rel: "noopener", textContent: "licence" }),
        " · xeno-canto.org",
      );
      credit.hidden = false;
      play(reference, "/listen/reference?what=audio&name=" + name, found.sonogram ? "/listen/reference?what=sonogram&name=" + name : "");
    };
  }

  // Beside the bird where there is room, so the bird stays in sight.
  const place = (spot) => {
    const gap = 16;
    const width = Math.min(352, innerWidth - 32);
    const right = spot.left + spot.width + gap;
    const left = right + width <= innerWidth - gap ? right : Math.max(gap, spot.left - gap - width);
    card.style.left = left + "px";
    card.style.top = Math.max(gap, Math.min(spot.top, innerHeight - gap - card.offsetHeight)) + "px";
  };

  player.addEventListener("ended", () => {
    for (const b of card.querySelectorAll(".buttons button")) b.setAttribute("aria-pressed", "false");
  });
  card.addEventListener("pointerdown", (e) => e.stopPropagation());
  // A tap anywhere else closes the card and goes on to the kiosk as before.
  addEventListener("pointerdown", (e) => { if (!card.hidden && !card.contains(e.target)) close(); }, true);
  addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  addEventListener("resize", build);
  collage.addEventListener("load", refresh);
  refresh();
}
