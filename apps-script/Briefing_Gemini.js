/**
 * LABMIND MASTER AUTOMATION (V31 - FULL PAYLOAD + RELEVANCE HANDOFF)
 * Role: "The Scout" (Gmail Crawler → GCS Trigger → Sheets Logger)
 *
 * SECURITY: All secrets loaded from Script Properties — never hardcoded.
 * Set these in: Project Settings → Script Properties
 *   GEMINI_API_KEY   → your AIzaSy... key
 *   RAW_PRIVATE_KEY  → your -----BEGIN PRIVATE KEY----- block
 *   CLIENT_EMAIL     → briefing-bot@gen-lang-client-0207242913.iam.gserviceaccount.com
 */

// ============================================================
// 1. CONFIGURATION
// ============================================================
const TARGET_FOLDER_ID  = '1bgxg4xfSUk53BBJ0n2TMJxinf_DzqB4-';
const SEARCH_QUERY      = 'from:googlealerts-noreply@google.com newer_than:7d';
const BUCKET_NAME       = 'labmind-briefing-ops';
const BRIEFING_SHEET_ID = '1ji1clDmv7YNARpY3TG39ADIC_pbCfdw_fPKVb3lEVLA';

// ============================================================
// 2. SECRETS LOADER
// All sensitive values come from Script Properties — never from code.
// ============================================================
function getSecrets() {
  const props = PropertiesService.getScriptProperties();
  return {
    GEMINI_API_KEY: props.getProperty('GEMINI_API_KEY'),
    RAW_PRIVATE_KEY: props.getProperty('RAW_PRIVATE_KEY'),
    CLIENT_EMAIL: props.getProperty('CLIENT_EMAIL'),
  };
}


// ============================================================
// 3. MAIN EXECUTION
// ============================================================
function compileWeeklyBriefing() {
  const secrets     = getSecrets();
  const historySet  = loadHistory();
  const threads     = GmailApp.search(SEARCH_QUERY, 0, 100);

  if (threads.length === 0) {
    Logger.log('No threads found matching query.');
    return;
  }

  const doc  = DocumentApp.create('LabMind Briefing - ' + new Date().toLocaleDateString());
  const body = doc.getBody();
  const freshArticles = [];

  threads.forEach((thread) => {
    try {
      const message  = thread.getMessages().pop();
      if (!isWithinLookbackWindow(message.getDate(), 7)) return;

      const articles = parseForensic(message.getBody());
      articles.forEach(item => {
        if (!historySet.has(item.url)) {
          freshArticles.push(item);
          historySet.add(item.url);
          body.appendParagraph(`• ${item.title}`).setLinkUrl(item.url);
        }
      });
    } catch (e) {
      Logger.log('Thread parse error: ' + e.message);
    }
  });

  if (freshArticles.length === 0) {
    Logger.log('No fresh articles found. Discarding doc.');
    DriveApp.getFileById(doc.getId()).setTrashed(true);
    return;
  }

  doc.saveAndClose();
  moveFileToFolder(doc.getId(), TARGET_FOLDER_ID);

  saveHistory(freshArticles);
  updateBriefingSheet(freshArticles);
  dispatchRAGJob(freshArticles, secrets);
}


// ============================================================
// 4. RAG JOB DISPATCHER
// ============================================================
function dispatchRAGJob(articles, secrets) {
  const uniqueArticles = [...new Map(articles.map(a => [a.url, a])).values()];

  const payload = {
    action:   'deep_dive_rag',
    articles: uniqueArticles.slice(0, 200),
    api_key:  secrets.GEMINI_API_KEY        // Loaded from Script Properties
  };

  Logger.log(`Dispatching RAG job with ${payload.articles.length} articles.`);
  uploadFileToGCS(JSON.stringify(payload), 'queue/trigger_job.json', 'application/json', secrets);
}


// ============================================================
// 5. GCS UPLINK (OAuth2 Authenticated)
// ============================================================
function uploadFileToGCS(content, filename, mimeType, secrets) {
  try {
    const service = getGcpService(secrets);
    if (!service.hasAccess()) {
      Logger.log('GCS Auth failed: ' + service.getLastError());
      return;
    }
    const token       = service.getAccessToken();
    const encodedName = encodeURIComponent(filename);

    const response = UrlFetchApp.fetch(
      `https://storage.googleapis.com/upload/storage/v1/b/${BUCKET_NAME}/o?uploadType=media&name=${encodedName}`,
      {
        method:             'POST',
        contentType:        mimeType,
        payload:            content,
        headers:            { Authorization: 'Bearer ' + token },
        muteHttpExceptions: true
      }
    );
    Logger.log('GCS Upload Status: ' + response.getResponseCode());
    if (response.getResponseCode() !== 200) {
      Logger.log('GCS Error Body: ' + response.getContentText());
    }
  } catch (e) {
    Logger.log('GCS Upload Exception: ' + e.message);
  }
}


// ============================================================
// 6. AUTHENTICATION HANDLER
// ============================================================
function getGcpService(secrets) {
  const cleanKey = secrets.RAW_PRIVATE_KEY.replace(/\\n/g, '\n').trim();

  return OAuth2.createService('GCP')
    .setTokenUrl('https://oauth2.googleapis.com/token')
    .setPrivateKey(cleanKey)
    .setIssuer(secrets.CLIENT_EMAIL)
    .setSubject(secrets.CLIENT_EMAIL)
    .setPropertyStore(PropertiesService.getScriptProperties())
    .setScope('https://www.googleapis.com/auth/cloud-platform');
}

function resetGCSAuth() {
  const secrets = getSecrets();
  getGcpService(secrets).reset();
  PropertiesService.getScriptProperties().deleteAllProperties();
  Logger.log('Authorization cleared. Re-run compileWeeklyBriefing to re-authenticate.');
}


// ============================================================
// 7. HISTORY (Persistence Layer)
// ============================================================
function loadHistory() {
  try {
    const sheet   = getOrInitDb();
    const lastRow = sheet.getLastRow();
    if (lastRow < 2) return new Set();
    const urls = sheet.getRange(2, 3, lastRow - 1, 1).getValues().flat().filter(Boolean);
    Logger.log(`History loaded: ${urls.length} seen URLs.`);
    return new Set(urls);
  } catch (e) {
    Logger.log('loadHistory error: ' + e.message);
    return new Set();
  }
}

function saveHistory(articles) {
  try {
    const sheet = getOrInitDb();
    const now   = new Date();
    const rows  = articles.map(a => [now, a.title || '', a.url || '']);
    if (rows.length > 0) {
      sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, 3).setValues(rows);
      Logger.log(`History saved: ${rows.length} new URLs persisted.`);
    }
    purgeOldHistory(sheet, 7);
  } catch (e) {
    Logger.log('saveHistory error: ' + e.message);
  }
}

function purgeOldHistory(sheet, days) {
  try {
    const lastRow = sheet.getLastRow();
    if (lastRow < 2) return;

    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - days);

    const timestamps = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
    let purgeCount   = 0;

    for (let i = timestamps.length - 1; i >= 0; i--) {
      const rowDate = new Date(timestamps[i][0]);
      if (!isNaN(rowDate) && rowDate < cutoff) {
        sheet.deleteRow(i + 2);
        purgeCount++;
      }
    }

    if (purgeCount > 0) {
      Logger.log(`History purged: ${purgeCount} rows older than ${days} days removed.`);
    }
  } catch (e) {
    Logger.log('purgeOldHistory error: ' + e.message);
  }
}

function getOrInitDb() {
  const files = DriveApp.searchFiles("title = 'LabMind_Link_DB' and trashed = false");
  if (files.hasNext()) {
    return SpreadsheetApp.open(files.next()).getSheets()[0];
  }
  const sheet = SpreadsheetApp.create('LabMind_Link_DB').getSheets()[0];
  sheet.appendRow(['Timestamp', 'Title', 'URL']);
  return sheet;
}


// ============================================================
// 8. BRIEFING SHEET UPDATER
// ============================================================
function updateBriefingSheet(articles) {
  try {
    const ss    = SpreadsheetApp.openById(BRIEFING_SHEET_ID);
    const sheet = ss.getSheets()[0];

    if (sheet.getLastRow() === 0) {
      sheet.appendRow(['Date Found', 'Article Title', 'Article URL']);
      sheet.getRange(1, 1, 1, 3).setFontWeight('bold').setBackground('#1a1a2e').setFontColor('#00ff41');
      sheet.setFrozenRows(1);
      Logger.log('Briefing sheet initialized with headers.');
    }

    const now  = new Date();
    const rows = articles.map(a => [now, a.title || '', a.url || '']);
    if (rows.length === 0) return;

    sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, 3).setValues(rows);
    sheet.autoResizeColumns(1, 3);
    Logger.log(`Briefing sheet updated: ${rows.length} rows appended.`);
  } catch (e) {
    Logger.log('updateBriefingSheet error: ' + e.message);
  }
}


// ============================================================
// 9. HELPERS
// ============================================================
function moveFileToFolder(fileId, folderId) {
  try {
    DriveApp.getFileById(fileId).moveTo(DriveApp.getFolderById(folderId));
  } catch (e) {
    Logger.log('moveFileToFolder error: ' + e.message);
  }
}

function parseForensic(html) {
  const results = [];
  const regex   = /<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/gi;
  let match;

  while ((match = regex.exec(html)) !== null) {
    let url   = match[1];
    let title = match[2].replace(/<[^>]+>/g, '').trim();

    if (url.includes('google.com/url')) {
      try {
        url = decodeURIComponent(url.split('url=')[1].split('&')[0]);
      } catch (e) {
        continue;
      }
    }

    if (!url.includes('google.com') && title.length > 0) {
      results.push({ title, url });
    }
  }
  return results;
}

function isWithinLookbackWindow(date, days) {
  const cutoff = new Date();
  cutoff.setHours(0, 0, 0, 0);
  cutoff.setDate(cutoff.getDate() - days);
  return new Date(date) >= cutoff;
}