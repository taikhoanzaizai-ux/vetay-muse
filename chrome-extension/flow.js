if (location.origin !== "https://muse.ai") {
(() => {
  const pairing = /^#vetay=([a-f0-9]{32})(?:-([0-9]{4,5}))?$/.exec(location.hash);
  if (window.__veTayRunning) return;
  window.__veTayRunning = true;
  const sleep = (ms) => new Promise(resolve => setTimeout(resolve,ms));
  const visible = node => node.getClientRects().length && getComputedStyle(node).visibility !== "hidden";
  const send = async (message) => {
    const response = await chrome.runtime.sendMessage(message);
    if (!response?.ok) throw Error(response?.error || "Mất kết nối với Vẽ Tay.");
    return response.result;
  };
  function resultImages(card) {
    return [...card.querySelectorAll("img")].filter(img => {
      if (!img.complete || !img.naturalWidth || !img.naturalHeight || !visible(img)) return false;
      const src = img.currentSrc || img.src;
      const url = new URL(src, location.href);
      const media = /^(Ô hiển thị hình ảnh của người dùng|User image tile)$/iu.test(img.alt.trim());
      const original = /^(blob:|https:\/\/[^/]*\.googleusercontent\.com\/)/i.test(src);
      return original || (media && ["https:", "data:", "blob:"].includes(url.protocol));
    });
  }
  const caption = button => (button.getAttribute("aria-label") || button.getAttribute("title") || button.textContent || "").trim();
  function promptField() {
    const fields = [...document.querySelectorAll('textarea,[contenteditable="true"],[contenteditable="plaintext-only"]')]
      .filter(field => visible(field) && !field.closest('[inert],[aria-hidden="true"],[role="search"]')
        && !field.disabled && !field.readOnly && field.getAttribute("aria-disabled") !== "true");
    const labels = field => ["aria-label", "placeholder", "data-placeholder", "title"]
      .map(name => field.getAttribute(name) || "").join(" ");
    const eligible = fields.filter(field => !/search|tìm kiếm|rename|đổi tên|project name|tên dự án/i.test(labels(field))
      && !fields.some(parent => parent !== field && parent.contains(field)));
    const matches = eligible.filter(field => /prompt|what (?:do|would) you|describe|mô tả|bạn muốn|tạo nội dung/i.test(labels(field)));
    if (matches.length === 1) return matches[0];
    return eligible.length === 1 ? eligible[0] : null;
  }
  const ratioLabel = ratio => new RegExp("(?:^|[^0-9])" + ratio.replace(":", "\\s*[:∶]\\s*") + "(?![0-9])", "u");
  async function selectRatio(ratio) {
    const selected = node => ["aria-checked", "aria-selected", "aria-pressed"].some(key => node.getAttribute(key) === "true")
      || node.getAttribute("data-state") === "checked" || node.getAttribute("data-state") === "active";
    const controls = () => [...document.querySelectorAll("button,[role=button],[role=radio],[role=option],[role=menuitemradio],[role=tab]")]
      .filter(node => visible(node) && !node.disabled && node.getAttribute("aria-disabled") !== "true");
    const labels = node => [caption(node), node.getAttribute("data-value") || "", node.innerText || ""].join(" ");
    const exact = ratioLabel(ratio);
    const iconName = "crop_" + ratio.replace(":", "_");
    const matchesRatio = node => exact.test(labels(node)) || [...node.querySelectorAll("mat-icon,[role=img],svg")]
      .some(icon => [icon.textContent, icon.getAttribute("aria-label"), icon.getAttribute("data-icon")]
        .some(label => label?.trim() === iconName));
    const hasRatio = ratioLabel("[0-9]+:[0-9]+");
    const isOption = node => ["radio", "option", "menuitemradio"].includes(node.getAttribute("role"));
    const trigger = controls().filter(node => !isOption(node) && (node.getAttribute("aria-haspopup")
      ? /aspect ratio|tỷ lệ|tỉ lệ/iu.test(labels(node)) || hasRatio.test(labels(node))
      : /^(?:aspect ratio|tỷ lệ(?: khung hình)?|tỉ lệ(?: khung hình)?)$/iu.test(caption(node))));
    const options = () => controls().filter(node => !trigger.includes(node) && node !== settings && matchesRatio(node));
    const modelSettings = controls().filter(node => /Nano Banana|Imagen/iu.test(labels(node)) && /[x×][1-4]\b/iu.test(labels(node)));
    const settings = controls().find(node => /^(Điều kiện kích hoạt cài đặt|Settings trigger|Generation settings)$/iu.test(caption(node)))
      || (modelSettings.length === 1 ? modelSettings[0] : null);
    let opened = false;
    if (!options().length && settings) {
      settings.click();
      opened = true;
      const image = await waitFor(() => controls().find(node => node.getAttribute("role") === "radio" && /^(Image|Hình ảnh|Ảnh)$/iu.test(caption(node))), 3);
      if (image && !selected(image)) image.click();
    }
    if (!options().length) await waitFor(() => options().length, 3);
    if (!options().length && trigger.length === 1) trigger[0].click();
    const option = await waitFor(() => {
      if (trigger.length === 1 && exact.test(labels(trigger[0])) && !options().length) return trigger[0];
      const matches = options();
      const radios = matches.filter(isOption);
      return radios.length === 1 ? radios[0] : matches.length === 1 ? matches[0] : null;
    }, 12);
    if (!option) throw Error("Không chọn được tỷ lệ " + ratio + " trong Flow (" + (settings ? "đã thấy nút cài đặt ảnh" : "chưa thấy nút cài đặt ảnh") + ").");
    if (!trigger.includes(option) && !selected(option)) option.click();
    const single = controls().filter(node => /^[x×]\s*1$/iu.test(caption(node)));
    if (single.length === 1 && !selected(single[0])) single[0].click();
    if (opened && options().length && settings.isConnected) settings.click();
  }
  async function run() {
    let appliedRatio;
    const batch = await send({op:"batch"});
    if (batch.done) return;
    const queued = [];
    const results = [];
    const cards = () => [...document.querySelectorAll('[role="group"][aria-label]')];
    const normalize = text => (text || "").replace(/\s+/gu, " ").trim();
    const initialCards = cards();
    let newestFirst = true;
    let lastSubmit = 0;
    function checkFlowBlock() {
      const notices = [...cards(), ...document.querySelectorAll('[role="alert"],[role="status"]')];
      if (notices.some(node => visible(node) &&
          /chúng tôi nhận thấy có hoạt động bất thường|we noticed some unusual activity|we detected unusual activity/i.test(node.innerText || "")))
        throw Error("Flow báo hoạt động bất thường. Vẽ Tay đã dừng gửi prompt. Hãy kiểm tra Flow trong Chrome trước khi thử lại.");
    }
    async function submitScene(job) {
      checkFlowBlock();
      const {prompt} = job;
      const agent = [...document.querySelectorAll('[role="checkbox"],input[type="checkbox"]')].find(node => visible(node)
        && /^(Tác nhân|Agent)$/iu.test((node.getAttribute("aria-label") || node.textContent || "").trim())
        && (node.checked || node.getAttribute("aria-checked") === "true"));
      if (agent) { agent.click(); await sleep(300); }
      let field = await waitFor(promptField, 25);
      if (!field) throw Error("Không xác định được ô prompt duy nhất. Mở trang tạo ảnh trong dự án Flow.");
      await send({op:"activate"});
      const ratio = /^Tạo một ảnh minh họa tỷ lệ (\d+:\d+) /u.exec(job.prompt)?.[1];
      if (ratio && ratio !== appliedRatio) {
        await selectRatio(ratio);
        appliedRatio = ratio;
        field = await waitFor(promptField, 10);
        if (!field) throw Error("Không thấy ô prompt sau khi chọn tỷ lệ.");
      }
      field.focus();
      if (field instanceof HTMLTextAreaElement) {
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,"value").set.call(field,job.prompt);
        field.dispatchEvent(new InputEvent("input",{bubbles:true,data:job.prompt,inputType:"insertText"}));
      } else {
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(field);
        selection.removeAllRanges();
        selection.addRange(range);
        if (!document.execCommand("insertText",false,job.prompt)) throw Error("Không điền được prompt trong Flow.");
      }
      await sleep(500);
      const buttons = [...document.querySelectorAll("button,[role=button]")].filter(b=>visible(b)&&!b.disabled&&b.getAttribute("aria-disabled")!=="true");
      const start = buttons.filter(button => /^(Bắt đầu tạo|Start generating|Start creating)$/iu.test(caption(button)));
      let generate = start.length === 1 ? start : buttons.filter(b=>/^(Generate Image|Create Image|Tạo hình ảnh|Tạo ảnh)$/i.test(caption(b)));
      if (generate.length === 0 && matching(/Nano Banana|Imagen/i).length === 1)
        generate = buttons.filter(b=>/^(Generate|Tạo)$/i.test(caption(b)));
      if (generate.length === 0) {
        const editor = field.closest("form") || field.parentElement;
        generate = buttons.filter(button => /^(arrow_forward|send|gửi|bắt đầu tạo|start generating|start creating)$/iu.test(caption(button))
          && (editor?.contains(button) || field.getBoundingClientRect().bottom <= button.getBoundingClientRect().bottom
            && Math.abs(field.getBoundingClientRect().right - button.getBoundingClientRect().right) < 100));
      }
      if (generate.length !== 1) throw Error("Không nhận diện được nút tạo ảnh. Kiểm tra chế độ Ảnh, model và hạn mức Flow.");
      const prior = new Set(cards());
      const editorBox = field.getBoundingClientRect();
      const nearbyArrows = buttons.filter(button => {
        const box = button.getBoundingClientRect();
        return box.top >= editorBox.bottom - 8 && box.top <= editorBox.bottom + 100
          && Math.abs(box.right - editorBox.right) < 100
          && ([...button.querySelectorAll("mat-icon,[data-icon],[aria-hidden=true],img")].some(icon =>
            /^(arrow_forward|send)$/i.test((icon.getAttribute("data-icon") || icon.getAttribute("alt") || icon.textContent || "").trim()))
            || /^(Bắt đầu tạo|Start generating|Start creating)$/iu.test(caption(button)));
      });
      const submit = nearbyArrows.length === 1 ? nearbyArrows[0] : generate[0];
      submit.scrollIntoView({block:"nearest", inline:"nearest"});
      const box = submit.getBoundingClientRect();
      const x = box.left + box.width / 2;
      const y = box.top + box.height / 2;
      if (!submit.contains(document.elementFromPoint(x,y))) throw Error("Nút tạo ảnh đang bị che.");
      await sleep(Math.max(0, 8000 - (Date.now() - lastSubmit)));
      checkFlowBlock();
      lastSubmit = Date.now();
      await send({op:"submit", job:job.job, x, y});
      const findCard = () => {
        const matches = cards().filter(card => !prior.has(card) &&
          (normalize(card.getAttribute("aria-label")) === normalize(prompt)
           || normalize(card.textContent).includes(normalize(prompt))));
        return matches.length === 1 ? matches[0] : null;
      };
      const card = await waitFor(() => { checkFlowBlock(); return findCard(); }, 20);
      if (!card) throw Error("Flow chưa xác nhận đã nhận prompt cảnh " + (job.index + 1) + ".");
      return {job, card, findCard};
    }
    for (const [offset, prompt] of batch.prompts.entries()) {
      const job = {job:batch.job, index:batch.index + offset, prompt};
      const entry = await submitScene(job);
      const card = entry.card;
      const currentCards = cards();
      newestFirst &&= currentCards.length === initialCards.length + queued.length + 1
        && currentCards[0] === card;
      queued.push(entry);
      await send({op:"progress", count:queued.length, stage:"submitted"});
    }
    const claimed = new Set();
    for (const original of queued) {
      const {job} = original;
      let entry = original;
      let result;
      for (let attempt = 0; attempt < 3; attempt++) {
        const {card, findCard} = entry;
        const status = await waitFor(() => {
        checkFlowBlock();
        const grid = cards();
        const replacement = attempt === 0 && newestFirst && initialCards.length === 0 && grid.length === queued.length
          && grid.every(item => resultImages(item).length === 1)
          ? grid[queued.length - 1 - queued.findIndex(entry => entry.card === card)] : null;
        const current = (card.isConnected && resultImages(card).length ? card : findCard()) || replacement || (card.isConnected ? card : null);
        if (!current || claimed.has(current)) return null;
        const message = normalize(current.innerText);
        if (/hạn mức|không đủ tín dụng|chính sách|unusual activity|policy violation|not enough credits|quota/i.test(message) && !resultImages(current).length)
          throw Error("Flow yêu cầu kiểm tra chính sách hoặc hạn mức ở cảnh " + (job.index + 1) + ".");
        if (/tạo quá nhanh|requesting generations too quickly|rate limit|generation failed|không thể tạo|tạo ảnh thất bại/i.test(message) && !resultImages(current).length)
          return {failed:true};
        const urls = [...new Set(resultImages(current).map(image => image.currentSrc || image.src))];
        if (urls.length !== 1) return null;
        claimed.add(current);
        return {card:current};
        }, 150);
        if (status?.card) { result = status.card; break; }
        if (!status?.failed || attempt === 2) break;
        await send({op:"progress", count:results.length + 1, stage:"retry"});
        await sleep(attempt === 0 ? 30000 : 60000);
        entry = await submitScene(job);
      }
      if (!result) throw Error("Không nhận được ảnh cảnh " + (job.index + 1) + ". Kiểm tra trạng thái cảnh trong Flow.");
      results.push({job, result});
      await send({op:"progress", count:results.length});
    }
    for (const {job, result:card} of results) {
      if (!card.isConnected) throw Error("Thẻ ảnh cảnh " + (job.index + 1) + " đã thay đổi.");
      const activateControl = async (node, clicks=1) => {
        await send({op:"activate"});
        node.scrollIntoView({block:"center", inline:"nearest"});
        const box = node.getBoundingClientRect();
        const x = box.left + box.width / 2, y = box.top + box.height / 2;
        if (!node.contains(document.elementFromPoint(x,y))) throw Error("Nút tải ảnh Flow đang bị che.");
        await send({op:"submit", job:job.job, x, y, clicks});
      };
      const tile = resultImages(card)[0];
      await activateControl(tile, 2);
      const download = await waitFor(() => matching(/^(Tải nội dung nghe nhìn xuống|Download media|Download)$/iu)[0], 12);
      if (!download) throw Error("Không thấy nút tải ảnh gốc của cảnh " + (job.index + 1) + ".");
      await activateControl(download);
      const option = await waitFor(() => [...document.querySelectorAll('[role="menuitem"],button')]
        .find(item => visible(item) && /^2K(?:\s|$)/iu.test(caption(item)) && !item.disabled && item.getAttribute("aria-disabled") !== "true"), 8);
      if (!option) throw Error("Flow không có tùy chọn tải 2K cho cảnh " + (job.index + 1) + ".");
      await send({op:"arm2k",job:job.job,index:job.index});
      await activateControl(option);
      const deadline = Date.now() + 150000;
      let received = false;
      while (!received && Date.now() < deadline) {
        received = (await send({op:"transfer2k",job:job.job,index:job.index})).received;
        if (!received) await sleep(1000);
      }
      if (!received) throw Error("Chrome chưa tải xong ảnh 2K cho cảnh " + (job.index + 1) + ".");
      if (job.index !== batch.index + batch.prompts.length - 1) {
        const back = await waitFor(() => matching(/^(Nút quay lại để quay về trang trước|Go back|Back)$/iu)[0], 8);
        if (!back) throw Error("Không thấy nút trở lại lưới ảnh Flow.");
        await activateControl(back);
        await waitFor(() => cards().length > 0, 12);
        for (const entry of results) {
          if (entry.result.isConnected) continue;
          const replacement = newestFirst && initialCards.length === 0 && cards().length === queued.length
            ? cards()[queued.length - 1 - (entry.job.index - batch.index)] : null;
          if (replacement) entry.result = replacement;
        }
      }
    }
  }
  async function waitFor(find, seconds) {
    for (let i=0;i<seconds*2;i++) {
      const value=find();
      if (value) return value;
      await sleep(500);
    }
    return null;
  }
  const projectOpen = () => /\/projects?\/[^/]+/.test(location.pathname);
  const matching = (pattern) => [...document.querySelectorAll("button,[role=button],[role=tab],a")]
    .filter(item=>visible(item)&&!item.disabled&&item.getAttribute("aria-disabled")!=="true"
      && (pattern.test(caption(item)) || pattern.test((item.innerText || item.textContent || "").replace(/\s+/gu, " ").trim())));
  async function connect() {
    const state = await send(pairing ? {op:"pair",token:pairing[1],port:Number(pairing[2] || 7877)} : {op:"resume"});
    if (state.done) return;
    if (pairing) history.replaceState(history.state, "", location.pathname + location.search); 
    if (document.readyState === "loading") await new Promise(resolve=>document.addEventListener("DOMContentLoaded",resolve,{once:true}));
    if (!projectOpen()) {
      if (!pairing) throw Error("Flow vẫn ở trang chủ sau khi tạo dự án. Kiểm tra đăng nhập rồi chạy lại.");
      const create = await waitFor(() => {
        const choices = matching(/^(?:(?:add(?:_circle|_box)?|plus|\+)\s*)?(?:New project|Create project|Create new project|Dự án mới|Tạo dự án|Tạo dự án mới)$/iu);
        const controls = choices.filter(item => !choices.some(other => other !== item && item.contains(other)));
        return controls[0] || null;
      },35);
      if (!create) throw Error("Không tìm thấy nút tạo dự án Flow. Kiểm tra Chrome đã đăng nhập và xem trang Flow có thông báo gì.");
      create.click();
      if (!await waitFor(projectOpen,45)) throw Error("Flow chưa mở dự án mới sau khi bấm tạo.");
    }
    if (!await waitFor(()=>document.querySelector('textarea,[contenteditable="true"]'),45))
      throw Error("Dự án mới chưa hiện ô prompt.");
    
    await run();
  }
  connect().catch(async error => {
    try {await send({op:"error",message:error.message});} catch { console.warn("Vẽ Tay: ghép nối hoặc gửi trạng thái thất bại.", error.message); }
  }).finally(()=>{window.__veTayRunning=false;});
})();
} else {
(() => {
  const match = /^#vetay=([a-f0-9]{32})-(\d{4,5})$/.exec(location.hash);
  const key = "vetay-muse-task";
  if (!match && !sessionStorage.getItem(key)) return;
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const label = node => (node.getAttribute("aria-label") || node.title || node.textContent || "").trim();
  const visible = node => node.getClientRects().length && !node.disabled && node.getAttribute("aria-disabled") !== "true";
  const controls = pattern => [...document.querySelectorAll("button,[role=button]")].filter(node => visible(node) && pattern.test(label(node)));
  const send = async message => {
    const response = await chrome.runtime.sendMessage(message);
    if (!response?.ok) throw Error(response?.error || "Mất kết nối với Vẽ Tay.");
    return response.result;
  };
  async function waitFor(find, ms, message) {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      const value = find();
      if (value) return value;
      await pause(500);
    }
    throw message instanceof Error ? message : Error(message);
  }
  function save(state) {
    sessionStorage.setItem(key, JSON.stringify(state));
  }
  class SceneError extends Error {}
  const tagFor = (job, index, attempt) =>
    "vetay" + job.job.slice(0, 12) + "c" + String(index + 1).padStart(5, "0") + "r" + attempt;
  async function advance(state) {
    const next = await send({op:"next"});
    if (next.done) {
      sessionStorage.removeItem(key);
    } else if (next.index < state.batchEnd) {
      save({phase:"library", job:next.job, index:next.index,
            tag:tagFor(next, next.index, 0), batchEnd:state.batchEnd, confirmed:state.confirmed,
            batchStart:state.batchStart, chatImages:state.chatImages, harvestDiag:state.harvestDiag});
      location.assign("https://muse.ai/library/media?view=images");
    } else {
      save({phase:"compose"});
      location.assign("https://muse.ai/");
    }
  }
  async function uploadChatImage(job, index, url) {
    // Tai mot anh tu dia chi trong khung chat va gui thang cho Ve Tay qua
    // worker (op "image" -> bridge /image). Khong qua Thu vien, khong qua
    // Chrome tai xuong. Luu y: worker phai cho phep op "image" trong tab
    // Muse - ban 1.3.38 tro ve truoc thieu op nay trong danh sach cho phep
    // nen moi lan du phong deu bi worker tu choi ngay lap tuc.
    const response = await fetch(url, {credentials:"include"});
    if (!response.ok) throw Error("Không tải được ảnh trong khung chat.");
    const blob = await response.blob();
    if (!blob.type.startsWith("image/") || blob.size > 24*1024*1024)
      throw Error("Ảnh trong khung chat không hợp lệ.");
    const data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
    if (!/^data:image\/(png|webp|jpeg);base64,/.test(data))
      throw Error("Ảnh trong khung chat không hợp lệ.");
    await send({op:"progress", count:index + 1, stage:"generated"});
    await send({op:"image", job:job.job, index, data});
  }
  async function chatUpload(state, job) {
    // Du phong cho giai doan Thu vien: gui canh hien tai tu khung chat.
    const offset = state.index - (state.batchStart ?? state.index);
    const url = state.chatImages?.[offset];
    if (!url) throw Error("Không có ảnh dự phòng trong khung chat.");
    await uploadChatImage(job, job.index, url);
    await advance(state);
  }
  async function run() {
    const state = match ? {phase:"compose"} : JSON.parse(sessionStorage.getItem(key));
    const job = await send(match ? {op:"pair", token:match[1], port:Number(match[2])} : {op:"resume"});
    if (job.done) {
      sessionStorage.removeItem(key);
      return;
    }
    if (job.provider !== "muse") throw Error("Tác vụ hiện tại không thuộc Muse.");
    if (!match && state.phase === "collect") {
      // Giua dot, job.index la so anh da nhan duoc nen nam giua roundStart
      // va roundEnd la hop le (tai lai trang giua chung van vao lai duoc).
      if (state.job !== job.job || job.index < state.roundStart || job.index > state.roundEnd)
        throw Error("Cảnh Muse đã thay đổi. Hãy bắt đầu lại từ Vẽ Tay.");
    } else if (!match && state.phase !== "compose" && (state.job !== job.job || state.index !== job.index))
      throw Error("Cảnh Muse đã thay đổi. Hãy bắt đầu lại từ Vẽ Tay.");
    if (match) {
      save(state);
      history.replaceState(history.state, "", location.pathname + location.search);
    }
    if (state.phase === "compose") {
      if (state.delay) {
        await send({op:"progress", count:job.index + 1, stage:state.attempt ? "retry" : "batch"});
        await pause(state.delay);
      }
      const findField = async () => {
        const found = await waitFor(() => {
          const editors = [...document.querySelectorAll('textarea,[contenteditable=true],[contenteditable=plaintext-only],input:not([type]),input[type=text],[role=textbox]')]
            .filter(node => visible(node) && !node.readOnly && !node.closest('[inert],[aria-hidden=true],[role=search],aside,nav,[role=dialog]'))
            .filter(node => node.matches('textarea,input') || node.isContentEditable);
          const bottom = editors.filter(node => node.getBoundingClientRect().bottom > innerHeight * 0.7);
          const named = bottom.filter(node => ['aria-label','placeholder','data-placeholder','title']
            .some(attr => /^(Nhắn tin|Message|Send a message|Type a message)(?:$|[.…])/iu.test((node.getAttribute(attr) || '').trim())));
          const choices = named.length ? named : bottom;
          return choices.filter(node => !choices.some(other => other !== node && node.contains(other)))
            .reduce((best, node) => !best || node.getBoundingClientRect().width > best.getBoundingClientRect().width ? node : best, null);
        }, 30000, "Không thấy ô Nhắn tin. Hãy đăng nhập Muse trong Chrome rồi thử lại.");
        if ((found.value ?? found.innerText ?? found.textContent).trim()) throw Error("Ô Nhắn tin Muse đang có bản nháp.");
        return found;
      };
      const batch = await send({op:"batch"});
      if (batch.done || batch.job !== job.job || batch.index !== job.index)
        throw Error("Danh sách cảnh Muse đã thay đổi.");
      const attempt = state.attempt || 0;
      // Gui mot prompt rieng le cho dung mot canh (1 tep TXT, 1 cau lenh) va
      // tra ve ngay sau khi Muse nhan tin — KHONG doi anh tao xong. Viec
      // cho va tai anh la cua result() (luot thu lai) hoac collectRound()
      // (dot 10 canh).
      const submitOne = async (description, index) => {
        const field = await findField();
        const tag = tagFor(job, index, attempt);
        const files = [new File([tag + "\n" + description], tag + ".txt", {type:"text/plain"})];
        const prompt = "Tạo đúng 1 ảnh trong lượt này, mỗi cảnh bên dưới một ảnh riêng. " +
          "Nếu có thể, chạy các cảnh song song. Lưu tất cả vào Thư viện hình ảnh. " +
          "Đặt tên mỗi ảnh bắt đầu bằng mã cảnh tương ứng, giữ nguyên mã. Không đưa mã vào nội dung ảnh. " +
          "Các tệp TXT đính kèm là bản sao của các prompt bên dưới, chỉ tạo một ảnh cho mỗi mã. " +
          "Khi xong, báo Hoàn tất và liệt kê các mã đã tạo. Mã đầu đợt: " + tag + "\n\n" +
          "Cảnh " + (index + 1) + " | " + tag + "\n" + description;
        field.focus();
      const data = new DataTransfer();
      files.forEach(file => data.items.add(file));
      field.dispatchEvent(new ClipboardEvent("paste", {clipboardData:data, bubbles:true, cancelable:true}));
      const composerText = () => {
        const namesIn = root => {
          if (!root) return false;
          const content = root.textContent || "";
          const labels = [...root.querySelectorAll('[aria-label],[title]')].map(el =>
            (el.getAttribute('aria-label') || '') + ' ' + (el.title || '')).join(" ");
          return files.every(file => (content + " " + labels).includes(file.name));
        };
        let node = field;
        for (let level = 0; node && level < 10; level++, node = node.parentElement) {
          if (namesIn(node)) return true;
        }
        // Chip tep co the render ngoai cay cha cua o nhap (portal). Ten tep la
        // duy nhat theo tung job/canh/lan thu nen quet ca trang khong the nham
        // sang tin nhan cu. Ban cu chi quet 7 tang cha, bo sot chip, khien
        // fallback gan lai qua o chon tep va moi tep bi dinh kem 2 lan.
        return namesIn(document.body);
      };
      let attached = false;
      try { await waitFor(composerText, 8000, "paste-ignored"); attached = true; } catch {}
      if (!attached) {
        // Muse co the bo qua su kien paste gia lap: thu lai bang o chon tep co san.
        const fileInput = [...document.querySelectorAll('input[type="file"]')].filter(node => !node.disabled)[0];
        if (fileInput) {
          const transfer = new DataTransfer();
          files.forEach(file => transfer.items.add(file));
          fileInput.files = transfer.files;
          fileInput.dispatchEvent(new Event("input", {bubbles:true}));
          fileInput.dispatchEvent(new Event("change", {bubbles:true}));
        }
        await waitFor(composerText, 20000, "Muse chưa nhận đủ " + files.length + " tệp. Đã dừng trước khi gửi.");
      }
      if (field instanceof HTMLTextAreaElement || field instanceof HTMLInputElement) {
        Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), "value").set.call(field, prompt);
        field.dispatchEvent(new InputEvent("input", {bubbles:true, data:prompt}));
      } else if (!document.execCommand("insertText", false, prompt)) {
        throw Error("Không điền được lệnh tạo ảnh Muse.");
      }
      const button = await waitFor(() => {
        const buttons = controls(/^(Gửi|Send|Send message)$/iu);
        return buttons.length === 1 ? buttons[0] : null;
      }, 8000, "Không tìm được nút Gửi của Muse.");
        button.click();
        await send({op:"progress", count:index + 1, stage:"submitted"});
        await waitFor(() => !(field.value ?? field.textContent).includes(tag), 12000, "Muse chưa nhận prompt.");
        return {tag, tail:prompt.slice(-48)};
      };
      if (attempt) {
        const sent = await submitOne(batch.prompts[0], job.index);
        const next = {phase:"await", job:job.job, index:job.index, tag:sent.tag, attempt, batchEnd:state.batchEnd, submittedAt:Date.now(), promptTail:sent.tail};
        save(next);
        return await result(next, job);
      }
      // CHE DO DOT 10 (tu ban 1.3.44 theo yeu cau cua nguoi dung): gui lien
      // tiep tung prompt rieng le cho 10 canh — prompt 1 gui xong thi gui
      // ngay prompt 2, khong doi anh tao xong — sau do collectRound cho ca
      // 10 anh xuat hien roi tai ca 10 ve Ve Tay, nghi 30 giay roi sang dot
      // tiep theo (prompt 11, 12, ... 20).
      const roundStart = job.index;
      const roundEnd = Math.min(roundStart + 10, roundStart + batch.prompts.length);
      const tags = [];
      let anchorTail = "";
      for (let offset = 0; offset < roundEnd - roundStart; offset++) {
        const sent = await submitOne(batch.prompts[offset], roundStart + offset);
        tags.push(sent.tag);
        if (offset === 0) anchorTail = sent.tail;
      }
      const collectState = {phase:"collect", job:job.job, index:roundStart, tag:tags[0], attempt:0,
        batchEnd:roundEnd, roundStart, roundEnd, tags, promptTail:anchorTail, submittedAt:Date.now()};
      save(collectState);
      return await collectRound(collectState, job);
    }
    if (state.phase === "await") return await result(state, job);
    if (state.phase === "collect") return await collectRound(state, job);
    if (state.phase !== "library" && state.phase !== "download") throw Error("Trạng thái Muse không hợp lệ.");
    const file = new URL(location.href).searchParams.get("file");
    if (!file || !file.split("/").pop().toLowerCase().includes(state.tag)) {
      let item = null;
      try {
        item = await waitFor(() => {
        const matches = node => ["alt", "title", "aria-label", "href", "data-filename", "data-file-name"]
          .some(attr => (node.getAttribute(attr) || "").toLowerCase().includes(state.tag));
        const eligible = node => visible(node) && !node.closest('[role="dialog"],dialog');
        const thumbnails = [...document.querySelectorAll("img")].filter(node => eligible(node) && matches(node));
        if (thumbnails.length === 1) return thumbnails[0];
        const named = [...document.querySelectorAll('[title],[aria-label],a[href],[data-filename],[data-file-name]')]
          .filter(node => eligible(node) && matches(node));
        const controls = [...new Set(named.map(node => node.closest('button,[role="button"],a') || node))];
        if (controls.length === 1) return controls[0];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        const found = new Set();
        while (walker.nextNode()) {
          const text = walker.currentNode;
          if (!text.nodeValue.trim().toLowerCase().startsWith(state.tag)) continue;
          const card = text.parentElement.closest('button,[role="button"],a');
          if (card && eligible(card)) found.add(card);
        }
        if (found.size === 1) return [...found][0];
        for (const grid of document.querySelectorAll('main, [role="main"], main div, [role="main"] div')) {
          if (grid.clientHeight > 150 && grid.scrollHeight > grid.clientHeight &&
              /^(auto|scroll)$/.test(getComputedStyle(grid).overflowY)) {
            const atEnd = grid.scrollTop + grid.clientHeight >= grid.scrollHeight - 2;
            grid.scrollTop = atEnd ? 0 : grid.scrollTop + Math.floor(grid.clientHeight * 0.8);
          }
        }
        return null;
        }, 60000, new SceneError("Không tìm được đúng ảnh của cảnh trong Muse."));
      } catch (error) {
        // Luoi Thu vien khong he lo ten tep (da thay o job fe77ec29: anh tao
        // dung ten ma tim 2 vong van khong thay o nao) -> dung du phong chat.
        try { await chatUpload(state, job); return; }
        catch (chatError) {
          if (state.chatImages?.length)
            throw Error(error.message + " | Dự phòng chat lỗi: " + chatError.message);
          throw error;
        }
      }
      item.click();
    }
    try {
      const button = await waitFor(() => {
      const file = new URL(location.href).searchParams.get("file");
      if (!file?.split("/").pop().toLowerCase().includes(state.tag)) return null;
      const buttons = controls(/^(Tải xuống|Download)$/iu).filter(node => node.closest('[role="dialog"],dialog'));
      return buttons.length === 1 ? buttons[0] : null;
    }, 12000, new SceneError("Không mở được ảnh đúng mã cảnh."));
    const filename = new URL(location.href).searchParams.get("file").split("/").pop();
    await send({op:"progress", count:job.index + 1, stage:"generated"});
    await send({op:"armMuse", job:job.job, index:job.index, filename});
    button.click();
    const deadline = Date.now() + 90000;
    while (Date.now() < deadline) {
      if ((await send({op:"transferMuse", job:job.job, index:job.index})).received) {
        await advance(state);
        return;
      }
      await pause(800);
    }
      throw new SceneError("Chrome chưa tải xong ảnh Muse.");
    } catch (error) {
      // Ca giai doan mo anh + tai xuong deu co du phong chat: ke ca khi tim
      // thay o anh nhung Chrome luu anh sai thu muc (tien ich khac chiem
      // quyen dat ten tep) thi van lay duoc anh ve cho Ve Tay.
      try { await chatUpload(state, job); return; }
      catch (chatError) {
        if (state.chatImages?.length)
          throw Error(error.message + " | Dự phòng chat lỗi: " + chatError.message);
        throw error;
      }
    }
  }
  function posAfter(anchorNode, el) {
    // el co nam sau moc anchorNode trong trang hay khong. Phan tu trong
    // shadow DOM thi so sanh qua phan tu chu (host) cua no.
    if (!anchorNode) return true;
    let node = el;
    const root = el.getRootNode();
    if (root instanceof ShadowRoot) node = root.host;
    try { return !!(anchorNode.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING); }
    catch { return true; }
  }
  function scanMediaCandidates() {
    // Quet sau toan trang, tra ve cac ung vien anh theo thu tu xuat hien:
    // the img thuong va trong shadow DOM mo, the video (anh bia/file anh),
    // phan tu dung anh nen CSS. Chi giu cai du lon de bo qua icon/avatar.
    // Ban 1.3.41 do duoc ca trang chat chi co 3 the <img> va khong cai nao
    // la anh vua tao (chat[img=3 lon=2 neo=1 max=1920x1080 thu=0] o job
    // 263ee73c2674c) nen phai quet ca cac dang the hien khac nay.
    let nImg = 0, nShadowImg = 0, nVid = 0, nBg = 0;
    const candidates = [];
    const walk = (rootNode, inShadow) => {
      for (const el of rootNode.querySelectorAll("*")) {
        if (el.tagName === "IMG") {
          if (inShadow) nShadowImg++; else nImg++;
          if (el.naturalWidth >= 100 && el.naturalHeight >= 100) {
            let url = el.currentSrc || el.src || "";
            const href = el.closest("a")?.href || "";
            if (/\.(webp|png|jpe?g)(\?|#|$)/iu.test(href)) url = href;
            candidates.push({el, url});
          }
        } else if (el.tagName === "VIDEO") {
          nVid++;
          const poster = el.poster || "";
          const src = el.currentSrc || el.src || "";
          const url = /\.(webp|png|jpe?g)(\?|#|$)/iu.test(poster) ? poster
            : (/\.(webp|png|jpe?g)(\?|#|$)/iu.test(src) ? src : "");
          if (url && (el.videoWidth >= 100 || poster)) candidates.push({el, url});
        } else {
          let bgUrl = "";
          try {
            const bg = getComputedStyle(el).backgroundImage || "";
            const m = bg.match(/url\(["']?(.*?)["']?\)/iu);
            if (m) bgUrl = m[1];
          } catch {}
          if (bgUrl) {
            nBg++;
            const rect = el.getBoundingClientRect();
            if (rect.width >= 100 && rect.height >= 100) candidates.push({el, url:bgUrl});
          }
        }
        if (el.shadowRoot) walk(el.shadowRoot, true);
      }
    };
    walk(document, false);
    return {candidates, nImg, nShadowImg, nVid, nBg, nFrame:document.querySelectorAll("iframe").length};
  }
  let lastHarvestDiag = "";
  function harvestChatImages(state, expectedCount) {
    // Thu dia chi cac anh vua tao trong khung chat. Neo vao cuoi prompt vua
    // gui: chi lay anh nam SAU moc nay trong trang, tranh nham anh cua cac
    // luot chat cu (cung nam trong doan chat chinh). Chi tra ve khi du so
    // luong cua batch; thieu la tra rong de khong bao gio anh xa sai canh.
    let anchor = null;
    if (state.promptTail) {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        if (walker.currentNode.nodeValue.includes(state.promptTail)) anchor = walker.currentNode;
      }
    }
    const scan = scanMediaCandidates();
    const seen = new Set();
    const found = [];
    for (const c of scan.candidates) {
      if (!posAfter(anchor, c.el)) continue;
      if (!/^(https|blob|data):/.test(c.url) || /avatar/iu.test(c.url) || seen.has(c.url)) continue;
      seen.add(c.url);
      found.push(c.url);
    }
    lastHarvestDiag = "chat[img=" + scan.nImg + " simg=" + scan.nShadowImg + " vid=" + scan.nVid +
      " bg=" + scan.nBg + " if=" + scan.nFrame + " neo=" + (anchor ? 1 : 0) +
      " lon=" + scan.candidates.length + " thu=" + found.length + "]";
    return found.length >= expectedCount ? found.slice(-expectedCount) : [];
  }
  async function result(state, job) {
    let confirmed = false;
    const expectedCount = state.attempt ? 1 : state.batchEnd - job.index;
    const tags = Array.from({length:expectedCount}, (_, offset) =>
      tagFor(job, job.index + offset, state.attempt || 0));
    const deadline = Date.now() + 245000;
    while (Date.now() < deadline) {
      const full = document.body?.innerText || "";
      // Chi xet phan trang SAU tin nhan minh vua gui. Ban than prompt co cau
      // "Khi xong, bao Hoan tat va liet ke cac ma da tao": neu quet ca prompt
      // thi tien ich se nhan nham la Muse da tao xong ngay khi vua bam Gui,
      // nhay sang Thu vien qua som trong khi chua co anh nao (ban 1.3.35 mac
      // loi nay vi mau quet qua rong "hoan tat.{0,80}da tao").
      let start = 0;
      if (state.promptTail) {
        const tailAt = full.lastIndexOf(state.promptTail);
        if (tailAt >= 0) start = tailAt + state.promptTail.length;
      }
      const text = full.slice(start);
      const at = text.lastIndexOf(state.tag);
      if (at >= 0) {
        const nearby = text.slice(Math.max(0, at - 6000), at + 16000);
        const after = text.slice(at);
        if (/hết (?:hạn mức|lượt)|đã đạt giới hạn|(?:weekly|usage|rate) limit|quota exceeded/iu.test(after))
          throw Error("Muse báo giới hạn sử dụng. Đã dừng gửi thêm prompt.");
        const complete = /hoàn tất\s*[—–:-]\s*đã tạo (?:xong|đúng)|đã tạo xong toàn bộ|đã tạo (?:xong|đúng) (?:toàn bộ|tất cả|\d+ ảnh)|completed\s*[—–:-]\s*(?:all|generated)/iu.test(nearby);
        // Tao anh that khong bao gio xong trong duoi 25 giay; khoa thoi gian
        // nay chan not moi nhan nham con sot lai tu noi dung phan chieu.
        const settled = !state.submittedAt || Date.now() - state.submittedAt >= 25000;
        if (settled && complete && tags.every(tag => nearby.includes(tag))) {
          confirmed = true;
          break;
        }
        if (/chưa được tạo trong lượt này|cần sang lượt tiếp theo|not generated in this batch/iu.test(after)) break;
        if (/không thể tạo ảnh|tạo ảnh thất bại|image generation failed/iu.test(after)) break;
      }
      await pause(1000);
    }
    // Thu dia chi cac anh vua tao ngay trong khung chat (chi khi Muse da
    // bao hoan tat that). Anh co the hien cham hon dong bao hoan tat vai
    // giay: job 85fd96c07ae5 (ban 1.3.39) xac nhan xong, quet mot lan thi
    // chua thu duoc anh nao nen phai lui ve Thu vien va ket o canh 2, trong
    // khi canh 1 tai duoc bang duong cu. Ban 1.3.40 cho toi da 30 giay,
    // quet lai moi 1.5 giay den khi du so anh cua batch.
    let chatImages = [];
    if (confirmed) {
      const harvestDeadline = Date.now() + 30000;
      while (true) {
        chatImages = harvestChatImages(state, expectedCount);
        if (chatImages.length >= expectedCount || Date.now() >= harvestDeadline) break;
        await pause(1500);
      }
    }
    if (confirmed && chatImages.length >= expectedCount) {
      // DUONG CHINH (tu ban 1.3.39): gui thang toan bo anh cua batch nay ngay
      // tu khung chat, khong sang Thu vien va khong tai xuong bang Chrome
      // nua. Duong Thu vien cu qua mong manh: luoi khong lo ten tep, con file
      // tai xuong thi bi tien ich khac chiem quyen dat ten khien Ve Tay tu
      // choi (loi "Chrome khong luu anh 2K dung thu muc cua tac vu"). Nguoi
      // dung da chap nhan dung anh ngay trong chat o chat luong tot nhat.
      const libraryState = {...state, phase:"library", confirmed, batchStart:job.index, chatImages, harvestDiag:lastHarvestDiag};
      save(libraryState);
      for (let offset = 0; offset < expectedCount; offset++) {
        await uploadChatImage(job, job.index + offset, chatImages[offset]);
      }
      await advance(libraryState);
      return;
    }
    // Muse can save images even when the completion message is absent.
    save({...state, phase:"library", confirmed, batchStart:job.index, chatImages, harvestDiag:lastHarvestDiag});
    location.assign("https://muse.ai/library/media?view=images");
  }
  function scanRound(tags) {
    // Chia trang chat thanh tung "vung" theo ma canh: vung cua canh k bat
    // dau tu tin nhan chua ma cua no va ket thuc o tin nhan chua ma canh ke
    // tiep. Anh nam trong vung cua canh nao la anh cua canh do — ke ca khi
    // mot canh bi loi tao anh, cac anh con lai khong bi xao tron thu tu.
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    const starts = tags.map(tag => nodes.find(node => (node.nodeValue || "").includes(tag)) || null);
    const scan = scanMediaCandidates();
    const seen = new Set();
    return tags.map((tag, k) => {
      const startNode = starts[k];
      if (!startNode) return {status:"pending", url:""};
      const endNode = k + 1 < tags.length ? starts[k + 1] : null;
      const si = nodes.indexOf(startNode);
      const ei = endNode ? nodes.indexOf(endNode) : nodes.length;
      let text = "";
      for (let i = si; i < ei; i++) text += (nodes[i].nodeValue || "") + " ";
      let url = "";
      for (const c of scan.candidates) {
        if (!posAfter(startNode, c.el)) continue;
        if (endNode && posAfter(endNode, c.el)) continue;
        if (!/^(https|blob|data):/.test(c.url) || /avatar/iu.test(c.url) || seen.has(c.url)) continue;
        seen.add(c.url);
        url = c.url;
        break;
      }
      let status = "pending";
      if (/chưa tạo được|chưa được tạo|không thể tạo|tạo ảnh thất bại|image generation failed/iu.test(text)) status = "fail";
      else if (/Mã đã tạo\s*:/u.test(text)) status = "ok";
      return {status, url};
    });
  }
  async function collectRound(state, job) {
    // Cho anh cua ca dot xuat hien trong khung chat roi tai tung anh ve Ve
    // Tay theo dung thu tu canh (bridge chi nhan anh theo thu tu). Canh nao
    // bi Muse bao loi tao anh thi ban giao cho co che Thu vien / thu lai /
    // bo qua san co xu ly tiep, khong keo chet ca dot. Moi 60 giay gui mot
    // progress de Ve Tay khong bao het thoi gian cho (no chi bao loi khi
    // 330 giay lien tiep khong nhan duoc tin hieu nao).
    const roundStart = state.roundStart, roundEnd = state.roundEnd;
    const total = roundEnd - roundStart;
    let uploaded = Math.max(0, job.index - roundStart);
    let lastKeepAlive = 0;
    const deadline = Date.now() + 20 * 60 * 1000;
    const handoff = (sceneOffset, confirmed) => {
      save({...state, phase:"library", index:roundStart + sceneOffset,
        tag:state.tags[sceneOffset], confirmed, batchStart:roundStart,
        chatImages:[], harvestDiag:lastHarvestDiag});
      location.assign("https://muse.ai/library/media?view=images");
    };
    while (Date.now() < deadline) {
      const regions = scanRound(state.tags);
      while (uploaded < total) {
        const region = regions[uploaded];
        if (region.status === "fail") { handoff(uploaded, false); return; }
        if (region.status !== "ok" || !region.url) break;
        await uploadChatImage(job, roundStart + uploaded, region.url);
        uploaded++;
      }
      if (uploaded >= total) {
        const next = await send({op:"next"});
        if (next.done) { sessionStorage.removeItem(key); return; }
        save({phase:"compose", delay:30000});
        location.assign("https://muse.ai/");
        return;
      }
      if (Date.now() - lastKeepAlive > 60000) {
        lastKeepAlive = Date.now();
        await send({op:"progress", count:roundEnd, stage:"submitted"});
      }
      await pause(5000);
    }
    const regions = scanRound(state.tags);
    handoff(uploaded, regions[uploaded]?.status === "ok");
  }
  run().catch(async error => {
    const state = JSON.parse(sessionStorage.getItem(key) || "null");
    try {
      if (error instanceof SceneError && state?.job) {
        if (state.phase === "library" && !state.libraryRetried) {
          save({...state, libraryRetried:true});
          location.replace("https://muse.ai/library/media?view=images");
          return;
        }
        if (state.confirmed || /tải xong|mở được ảnh/iu.test(error.message))
          throw Error("Không lấy được ảnh có sẵn của cảnh " + (state.index + 1) + " (" + state.tag + "). " + error.message +
            (state.harvestDiag ? " " + state.harvestDiag : ""));
        if (!state.attempt) {
          save({phase:"compose", attempt:1, delay:20000, batchEnd:state.batchEnd});
          location.assign("https://muse.ai/");
        } else {
          await send({op:"skipMuse", job:state.job, index:state.index});
          await advance(state);
        }
        return;
      }
      throw error;
    } catch (failure) {
      sessionStorage.removeItem(key);
      try { await send({op:"error", message:failure.message}); } catch {}
    }
  });
})();

}
