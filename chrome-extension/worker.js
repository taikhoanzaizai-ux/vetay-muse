const base = "http://127.0.0.1:7877";
function flowTab(url) { return /^https:\/\/(flow\.google\.com|labs\.google)\//.test(url || ""); }
async function api({token, port}, path, body) {
  const r = await fetch("http://127.0.0.1:" + port + path, {
    headers: {"X-VeTay-Token":token, ...(body ? {"Content-Type":body.type || "application/json"} : {})},
    method: body ? "POST" : "GET", body
  });
  if (!r.ok) throw Error((await r.text()).slice(0,250));
  return r.json();
}
const pendingDownloads = new Map();
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  const pending = [...pendingDownloads.values()].filter(entry => !entry.id && Date.now() < entry.deadline);
  if (pending.length !== 1) return;
  const entry = pending[0];
  const urls = [item.url, item.finalUrl].filter(Boolean);
  const isImage = /^image\/(?:png|jpeg|webp)$/i.test(item.mime || "")
    || /\.(png|jpe?g|webp)$/i.test(item.filename || "");
  const fromFlow = item.referrer === entry.page
    || urls.some(url => /^blob:https:\/\/flow\.google\.com\//i.test(url))
    || (isImage && !item.referrer && urls.some(url =>
      /^https:\/\/(?:[a-z0-9-]+\.)*(?:googleusercontent\.com|gstatic\.com)\//i.test(url)));
  const muse = entry.filename !== undefined;
  // Ten tep Muse chua ma canh vetay duy nhat cua dung canh dang cho, nen chi can
  // khop ten tep (ke ca khi Chrome them " (1)" luc trung ten). Dieu kien cu doi
  // referrer phai bang nguyen URL trang xem anh va URL phai thuoc muse.ai, khien
  // anh tai qua CDN / referrer rut gon bi bo qua va VeTay khong bao gio nhan anh.
  const baseName = name => (name || "").split(/[\\/]/).pop().toLowerCase().replace(/ \(\d+\)(\.[^.]+)$/, "$1");
  const fromMuse = muse && isImage && baseName(item.filename) === entry.filename.toLowerCase();
  if (muse ? !fromMuse : !fromFlow) return;
  const extension = /\.(png|jpe?g|webp)$/i.exec(item.filename || "")?.[1] || "png";
  entry.id = item.id;
  suggest({filename:(muse ? "VeTay-Muse/" : "VeTay-2K/") + "" + entry.stem + "." + extension, conflictAction:"overwrite"});
});
chrome.runtime.onMessage.addListener((m,sender,reply) => {
  (async()=>{
    const muse = sender.frameId === 0 && sender.tab?.url?.startsWith("https://muse.ai/");
    if (!muse && !flowTab(sender.tab?.url)) throw Error("Tab không thuộc Google Flow hoặc Muse.");
    const key = "pairing:" + sender.tab.id;
    if (m.op === "pair") {
      if (!/^[a-f0-9]{32}$/.test(m.token || "")) throw Error("Mã kết nối không hợp lệ.");
      const port = m.port ?? 7877;
      if (!Number.isInteger(port) || port < 1024 || port > 65535) throw Error("Cổng Vẽ Tay không hợp lệ.");
      const connection = {token:m.token, port};
      const job = await api(connection, "/next");
      if (job.done) throw Error("Vẽ Tay chưa có cảnh chờ.");
      if (muse !== (job.provider === "muse")) throw Error("Nguồn ảnh không khớp tác vụ.");
      await chrome.storage.session.set({[key]:connection});
      return job;
    }
    const saved = await chrome.storage.session.get(key);
    const pairing = typeof saved[key] === "string" ? {token:saved[key],port:7877} : saved[key];
    if (m.op === "resume") return pairing ? api(pairing,"/next") : {done:true};
    if (!pairing) throw Error("Tab chưa được Vẽ Tay kết nối.");
    if (muse && !["next","batch","progress","error","armMuse","transferMuse","skipMuse","image"].includes(m.op))
      throw Error("Thao tác không hợp lệ trong tab Muse.");
    if (m.op === "skipMuse") {
      const job = await api(pairing, "/next");
      if (!muse || job.provider !== "muse" || job.job !== m.job || job.index !== m.index)
        throw Error("Cảnh Muse cần bỏ qua không khớp.");
      pendingDownloads.delete(key);
      return api(pairing, "/skipMuse/" + job.job + "/" + job.index, "{}");
    }
    if (m.op === "armMuse" || m.op === "transferMuse") {
      const job = await api(pairing, "/next");
      if (!muse || job.provider !== "muse" || job.job !== m.job || job.index !== m.index)
        throw Error("Tác vụ ảnh Muse đã đổi.");
      if (m.op === "armMuse") {
        const page = new URL(sender.tab.url);
        const filename = page.searchParams.get("file")?.split("/").pop();
        const tag = "vetay" + job.job.slice(0, 12) + "c" + String(job.index + 1).padStart(5, "0");
        if (page.pathname !== "/library/media" || filename !== m.filename ||
            !filename?.toLowerCase().includes(tag) || !/\.(png|jpe?g|webp)$/i.test(filename))
          throw Error("Ảnh đang mở không khớp mã cảnh Muse.");
        pendingDownloads.set(key, {stem: job.job + "-" + job.index, filename,
                                   page: sender.tab.url, deadline: Date.now() + 90000});
        return {armed:true};
      }
      const entry = pendingDownloads.get(key);
      if (!entry?.id) return {received:false};
      const [item] = await chrome.downloads.search({id:entry.id});
      if (item?.state === "interrupted") throw Error("Chrome đã ngắt lượt tải ảnh Muse.");
      if (item?.state !== "complete") return {received:false};
      const result = await api(pairing, "/downloadMuse/" + job.job + "/" + job.index,
                               JSON.stringify({path:item.filename}));
      pendingDownloads.delete(key);
      return result;
    }
    if (m.op === "arm2k" || m.op === "transfer2k") {
      const job = await api(pairing,"/next");
      if (job.done || job.job !== m.job || job.index !== m.index) throw Error("Tác vụ ảnh 2K đã đổi.");
      if (m.op === "arm2k") {
        pendingDownloads.set(key,{stem:job.job + "-" + job.index, deadline:Date.now()+150000,page:sender.tab.url});
        return {armed:true};
      }
      const entry = pendingDownloads.get(key);
      if (!entry?.id) return {received:false};
      const [item] = await chrome.downloads.search({id:entry.id});
      if (item?.state === "interrupted") throw Error("Chrome đã ngắt lượt tải ảnh 2K.");
      if (item?.state !== "complete") return {received:false};
      const result = await api(pairing,"/download2k/" + job.job + "/" + job.index,JSON.stringify({path:item.filename}));
      pendingDownloads.delete(key);
      return result;
    }
    if (m.op === "submit") {
      const tab = await chrome.tabs.get(sender.tab.id);
      const page = new URL(tab.url);
      if (sender.frameId !== 0 || page.origin !== "https://flow.google.com"
        || !/^\/project\/[^/]+(?:\/.*)?$/.test(page.pathname))
        throw Error("Lệnh bấm không đến từ màn dự án hoặc màn xem ảnh Flow.");
      if (![m.x,m.y].every(Number.isFinite) || m.x < 0 || m.y < 0 || m.x > 10000 || m.y > 10000)
        throw Error("Nút Flow nằm ngoài vùng có thể bấm.");
      const job = await api(pairing,"/next");
      if (job.done || job.job !== m.job) throw Error("Tác vụ Vẽ Tay đã đổi.");
      const target = {tabId:tab.id};
      await chrome.debugger.attach(target,"1.3");
      try {
        const input = (type,buttons) => chrome.debugger.sendCommand(target,"Input.dispatchMouseEvent",
          {type,x:m.x,y:m.y,button:"left",buttons,clickCount:1});
        await input("mousePressed",1);
        await input("mouseReleased",0);
        if (m.clicks === 2) {
          const second = (type,buttons) => chrome.debugger.sendCommand(target,"Input.dispatchMouseEvent",
            {type,x:m.x,y:m.y,button:"left",buttons,clickCount:2});
          await second("mousePressed",1);
          await second("mouseReleased",0);
        }
      } finally {
        await chrome.debugger.detach(target);
      }
      return {clicked:true};
    }
    if (m.op === "activate") {
      await chrome.tabs.update(sender.tab.id, {active:true});
      await chrome.windows.update(sender.tab.windowId, {focused:true});
      return {active:true};
    }
    if (m.op === "next") return api(pairing,"/next");
    if (m.op === "batch") return api(pairing,"/batch");
    if (m.op === "progress") return api(pairing,"/progress",JSON.stringify({count:m.count,stage:m.stage}));
    if (m.op === "error") return api(pairing,"/error",JSON.stringify({message:m.message}));
    if (m.op === "image" && /^[a-f0-9]{32}$/.test(m.job) && Number.isInteger(m.index)) {
      let image;
      if (m.data && /^data:image\/(png|webp|jpeg);base64,/.test(m.data)) {
        image = await (await fetch(m.data)).blob();
      } else {
        const u = new URL(m.url);
        if (u.protocol !== "https:" || !/(^|\.)googleusercontent\.com$/.test(u.hostname)) throw Error("Không xác định được địa chỉ ảnh kết quả.");
        const r = await fetch(u.href,{credentials:"omit",redirect:"error"});
        if (!r.ok) throw Error("Không tải được ảnh kết quả.");
        image = await r.blob();
      }
      if (image.size > 24*1024*1024 || !image.type.startsWith("image/")) throw Error("Ảnh kết quả không hợp lệ.");
      return api(pairing,"/image/"+m.job+"/"+m.index,image);
    }
    throw Error("Yêu cầu không hợp lệ.");
  })().then(result=>reply({ok:true,result}),error=>reply({ok:false,error:error.message}));
  return true;
});
chrome.tabs.onRemoved.addListener(tabId => chrome.storage.session.remove("pairing:" + tabId));

const extensionCheck = "vetay-check-local-update";
function registerCheck() {
  chrome.alarms.create(extensionCheck, {periodInMinutes:1});
}
chrome.runtime.onInstalled.addListener(registerCheck);
chrome.runtime.onStartup.addListener(registerCheck);
chrome.alarms.onAlarm.addListener(async alarm => {
  if (alarm.name !== extensionCheck) return;
  try {
    const current = chrome.runtime.getManifest();
    const response = await fetch(chrome.runtime.getURL("manifest.json") + "?t=" + Date.now(), {cache:"no-store"});
    if (!response.ok) return;
    const disk = await response.json();
    if (!/^\d+\.\d+\.\d+$/.test(disk.version) || disk.name !== current.name ||
        disk.manifest_version !== current.manifest_version) return;
    const old = current.version.split(".").map(Number);
    const next = disk.version.split(".").map(Number);
    const changed = next.findIndex((value,i)=>value!==old[i]);
    if (changed < 0 || next[changed] < old[changed]) return;
    for(const key of ["permissions","host_permissions","optional_permissions","optional_host_permissions"]) {
      if(JSON.stringify([...(current[key]||[])].sort())!==JSON.stringify([...(disk[key]||[])].sort())) return;
    }
    const matches = value => (value.content_scripts || []).flatMap(script=>script.matches || []).sort();
    if(JSON.stringify(matches(current))!==JSON.stringify(matches(disk))) return;
    const idle = await fetch(base+"/extension-idle",{cache:"no-store"});
    if(idle.ok && (await idle.json()).idle === true) chrome.runtime.reload();
  } catch {}
});
