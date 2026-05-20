(function () {
  'use strict';

  var root = document.documentElement;
  var themeKey = 'loki:theme';
  var prefersDark = window.matchMedia('(prefers-color-scheme: dark)');

  function currentTheme() {
    var explicit = root.getAttribute('data-theme');
    if (explicit === 'light' || explicit === 'dark') return explicit;
    return 'dark';
  }

  function syncToggleBtn() {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    var theme = currentTheme();
    btn.setAttribute('aria-pressed', String(theme === 'dark'));
    btn.setAttribute('aria-label', theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
  }

  function initThemeToggle() {
    var btn = document.getElementById('theme-toggle');
    syncToggleBtn();
    if (btn) {
      btn.addEventListener('click', function () {
        var next = currentTheme() === 'dark' ? 'light' : 'dark';
        root.setAttribute('data-theme', next);
        try { localStorage.setItem(themeKey, next); } catch (e) {}
        syncToggleBtn();
      });
    }
    if (prefersDark.addEventListener) {
      prefersDark.addEventListener('change', function () {
        try { if (localStorage.getItem(themeKey)) return; } catch (e) {}
        syncToggleBtn();
      });
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    initThemeToggle();
    init();
  });

  function init() {
    var tbody = document.getElementById('loki-tbody');
    var lightbox = document.getElementById('lightbox');
    var lightboxImg = document.getElementById('lightbox-img');
    var lightboxCaption = document.getElementById('lightbox-caption');
    var lightboxClose = document.getElementById('lightbox-close');
    var lightboxBackdrop = document.getElementById('lightbox-backdrop');
    var lbZoomIn = document.getElementById('lb-zoom-in');
    var lbZoomOut = document.getElementById('lb-zoom-out');
    var lbZoomReset = document.getElementById('lb-zoom-reset');
    var lbZoomLevel = document.getElementById('lb-zoom-level');

    var expandedIdx = -1;
    var cache = {};
    var lbOpen = false;
    var lbZoom = 1;
    var lbPanX = 0, lbPanY = 0, panning = false, panStartX, panStartY;

    function esc(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
    function uid() { return '_' + Math.random().toString(36).substr(2,8); }
    function isNumArr(a) { for (var i=0;i<Math.min(a.length,10);i++){if(typeof a[i]!=='number')return false;} return true; }

    function jsonTree(val, key, depth, last) {
      var comma = last ? '' : ',';
      var sp = ''; for(var i=0;i<depth;i++) sp+='  ';
      var kp = key !== null ? '<span class="jk">"'+esc(key)+'"</span>: ' : '';

      if(val===null) return sp+kp+'<span class="jnull">null</span>'+comma;
      if(typeof val==='boolean') return sp+kp+'<span class="jb">'+val+'</span>'+comma;
      if(typeof val==='number') return sp+kp+'<span class="jn">'+val+'</span>'+comma;
      if(typeof val==='string') return sp+kp+'<span class="js">"'+esc(val)+'"</span>'+comma;

      if(Array.isArray(val)){
        if(!val.length) return sp+kp+'<span class="jbracket">[]</span>'+comma;
        if(isNumArr(val)&&val.length>8){
          var id=uid();
          var pv=val.slice(0,6).map(function(v){return '<span class="jn">'+v+'</span>';}).join(', ');
          var preview=sp+'<span class="jtoggle" data-t="'+id+'">\u25B6</span> '+kp+'<span class="jbracket">[</span>'+pv+', <span class="jcollapsed-hint">\u2026 '+(val.length-6)+' more</span>';
          var allVals=[];
          for(var ni=0;ni<val.length;ni++){
            allVals.push(sp+'  <span class="jn">'+val[ni]+'</span>'+(ni<val.length-1?',':''));
          }
          var block='<span class="jtree-block jcollapsed" id="'+id+'">\n'+allVals.join('\n')+'\n'+sp+'<span class="jbracket">]</span>'+comma+'</span>';
          return preview+'\n'+block+'<span class="jbracket jcollapsed-close" data-t="'+id+'">]</span>'+comma;
        }
        var id=uid();
        var hdr=sp+'<span class="jtoggle" data-t="'+id+'">\u25BC</span> '+kp+'<span class="jbracket">[</span> <span class="jcollapsed-hint">'+val.length+' items</span>';
        var ch=[];for(var ai=0;ai<val.length;ai++) ch.push(jsonTree(val[ai],null,depth+1,ai===val.length-1));
        return hdr+'\n<span class="jtree-block" id="'+id+'">'+ch.join('\n')+'\n'+sp+'<span class="jbracket">]</span>'+comma+'</span>';
      }

      if(typeof val==='object'){
        var ks=Object.keys(val);
        if(!ks.length) return sp+kp+'<span class="jbracket">{}</span>'+comma;
        var oid=uid();
        var ohdr=sp+'<span class="jtoggle" data-t="'+oid+'">\u25BC</span> '+kp+'<span class="jbracket">{</span> <span class="jcollapsed-hint">'+ks.length+' keys</span>';
        var oc=[];for(var ki=0;ki<ks.length;ki++) oc.push(jsonTree(val[ks[ki]],ks[ki],depth+1,ki===ks.length-1));
        return ohdr+'\n<span class="jtree-block" id="'+oid+'">'+oc.join('\n')+'\n'+sp+'<span class="jbracket">}</span>'+comma+'</span>';
      }
      return sp+String(val)+comma;
    }

    function bindToggles(el) {
      el.querySelectorAll('.jtoggle').forEach(function(t){
        t.addEventListener('click',function(e){
          e.stopPropagation();
          var tid=this.dataset.t;
          var block=document.getElementById(tid);
          if(!block)return;
          var hint=this.parentElement.querySelector('.jcollapsed-hint');
          var closeBracket=el.querySelector('.jcollapsed-close[data-t="'+tid+'"]');
          if(block.classList.contains('jcollapsed')){
            block.classList.remove('jcollapsed');this.textContent='\u25BC';
            if(hint)hint.style.display='none';
            if(closeBracket)closeBracket.style.display='none';
          }else{
            block.classList.add('jcollapsed');this.textContent='\u25B6';
            if(hint)hint.style.display='inline';
            if(closeBracket)closeBracket.style.display='inline';
          }
        });
      });
      el.querySelectorAll('.jcollapsed-hint').forEach(function(h){h.style.display='none';});
      el.querySelectorAll('.jcollapsed-close').forEach(function(c){c.style.display='none';});
      el.querySelectorAll('.jtree-block.jcollapsed').forEach(function(b){
        var tid=b.id;
        var hint=el.querySelector('.jtoggle[data-t="'+tid+'"]');
        if(hint)hint.parentElement.querySelector('.jcollapsed-hint').style.display='inline';
        var closeBracket=el.querySelector('.jcollapsed-close[data-t="'+tid+'"]');
        if(closeBracket)closeBracket.style.display='inline';
      });
    }

    function buildTable(records) {
      var currentHeight = tbody.offsetHeight;
      tbody.style.minHeight = currentHeight + 'px';
      tbody.innerHTML='';
      expandedIdx=-1;
      records.forEach(function(rec,idx){
        var tr=document.createElement('tr');
        tr.className='data-row';
        var d = cache[rec.index];
        var age = d ? (d.patient||{}).age_years||'\u2014' : '\u2014';
        var gender = d ? (d.patient||{}).gender||'\u2014' : '\u2014';
        var hr = d ? (d.measurements||{}).heart_rate_bpm||'\u2014' : '\u2014';
        var qrs = d ? (d.measurements||{}).qrs_duration_ms||'\u2014' : '\u2014';
        var interp = d ? (d.interpretation||{}) : {};
        var diagHtml = '\u2014';
        if (d) {
          if (interp.diseased) diagHtml='<span class="badge-diseased">'+esc(interp.label||'\u2014')+'</span>';
          else diagHtml='<span class="badge-normal">'+esc(interp.label||'\u2014')+'</span>';
        }
        tr.innerHTML=
          '<td class="td-num">'+String(rec.index).padStart(2,'0')+'</td>'+
          '<td class="td-id">'+esc(rec.stem.replace(/_ecg$/,'').replace(/^\d+\.\s*/,''))+'</td>'+
          '<td>'+esc(String(age))+'</td><td class="td-gender">'+esc(String(gender))+'</td><td class="td-diagnosis">'+diagHtml+'</td><td>'+esc(String(hr))+'</td><td>'+esc(String(qrs))+'</td>'+
          '<td class="td-expand"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></td>';
        tr.addEventListener('click',function(){toggle(rec,idx,tr);});
        tbody.appendChild(tr);

        var det=document.createElement('tr');
        det.className='detail-row';det.id='det-'+idx;
        var docTile = '';
        if (rec.doc_html) {
          var docUrl = '/loki_dashboard/static/src/portal/docs/'+encodeURIComponent(rec.doc_html)+'?v='+(rec.doc_html_v || Date.now());
          var dlUrl = rec.doc_docx ? '/loki_dashboard/static/src/portal/docs/'+encodeURIComponent(rec.doc_docx)+'?v='+(rec.doc_docx_v || Date.now()) : '';
          docTile =
            '<div class="detail-doc" data-doc="'+esc(docUrl)+'" data-doc-name="'+esc(rec.doc_html)+'"'+
            (dlUrl ? ' data-doc-dl="'+esc(dlUrl)+'"' : '')+'>'+
              '<div class="doc-card">'+
                '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+
                  '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'+
                  '<polyline points="14 2 14 8 20 8"/>'+
                  '<line x1="8" y1="13" x2="16" y2="13"/>'+
                  '<line x1="8" y1="17" x2="14" y2="17"/>'+
                '</svg>'+
                '<div class="doc-meta">'+
                  '<div class="doc-title">QC Report</div>'+
                  '<div class="doc-sub">Click to preview</div>'+
                '</div>'+
              '</div>'+
            '</div>';
        }
        var jsonPanel =
          '<div class="detail-json">'+
            '<div class="json-toolbar">'+
              '<button type="button" class="json-copy" aria-label="Copy JSON" title="Copy JSON">'+
                '<svg class="ico-copy" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+
                  '<rect x="9" y="9" width="13" height="13" rx="1"/>'+
                  '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'+
                '</svg>'+
                '<svg class="ico-check" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+
                  '<polyline points="20 6 9 17 4 12"/>'+
                '</svg>'+
                '<span class="json-copy-label">Copy</span>'+
              '</button>'+
            '</div>'+
            '<pre></pre>'+
          '</div>';
        det.innerHTML='<td colspan="8"><div class="detail-content">'+jsonPanel+'<div class="detail-right">'+docTile+'<div class="detail-image"><img src="/loki_dashboard/static/src/portal/img/'+encodeURIComponent(rec.png_file)+'" alt="ECG" draggable="false"/><span class="img-hint">Click to view</span></div></div></div></td>';
        tbody.appendChild(det);
      });
      tbody.style.minHeight = '';
    }

    var allRecords = [];
    var sortField = 'index';
    var sortDir = 'asc';

    function getSortValue(rec, field) {
      var d = cache[rec.index] || {};
      var p = d.patient || {};
      var m = d.measurements || {};
      var interp = d.interpretation || {};
      switch (field) {
        case 'index': return rec.index;
        case 'record': return rec.stem;
        case 'age': return p.age_years || 0;
        case 'gender': return p.gender || '';
        case 'diagnosis': return interp.label || '';
        case 'hr': return m.heart_rate_bpm || 0;
        case 'qrs': return m.qrs_duration_ms || 0;
        default: return rec.index;
      }
    }

    function sortAndRebuild() {
      var sorted = allRecords.slice();
      sorted.sort(function(a, b) {
        var va = getSortValue(a, sortField);
        var vb = getSortValue(b, sortField);
        if (va < vb) return sortDir === 'asc' ? -1 : 1;
        if (va > vb) return sortDir === 'asc' ? 1 : -1;
        return 0;
      });
      buildTable(sorted);
      updateSortHeaders();
    }

    function updateSortHeaders() {
      var ths = document.querySelectorAll('.loki-table th[data-sort]');
      ths.forEach(function(th) {
        th.classList.remove('sort-asc', 'sort-desc');
        if (th.dataset.sort === sortField) {
          th.classList.add(sortDir === 'asc' ? 'sort-asc' : 'sort-desc');
        }
      });
    }

    function initSortHeaders() {
      var ths = document.querySelectorAll('.loki-table th[data-sort]');
      ths.forEach(function(th) {
        th.addEventListener('click', function() {
          var field = th.dataset.sort;
          if (sortField === field) {
            sortDir = sortDir === 'asc' ? 'desc' : 'asc';
          } else {
            sortField = field;
            sortDir = 'asc';
          }
          sortAndRebuild();
        });
      });
    }

    function loadAllAndBuild(records) {
      allRecords = records;
      var loaded = 0;
      records.forEach(function(rec) {
        fetch('/loki/api/record/'+rec.index).then(function(r){return r.json();}).then(function(d){
          cache[rec.index]=d;
          loaded++;
          if (loaded === records.length) {
            applyFilters();
            initSortHeaders();
            initViewerControls();
          }
        }).catch(function(){
          loaded++;
          if (loaded === records.length) {
            applyFilters();
            initSortHeaders();
            initViewerControls();
          }
        });
      });
    }

    function applyFilters() {
      var searchEl = document.getElementById('viewer-search');
      var filterEl = document.getElementById('viewer-filter');
      var sortEl = document.getElementById('viewer-sort');
      var query = (searchEl ? searchEl.value : '').toLowerCase();
      var filter = filterEl ? filterEl.value : '';

      if (sortEl) sortField = sortEl.value;

      var filtered = allRecords.filter(function(rec) {
        var d = cache[rec.index] || {};
        var interp = d.interpretation || {};
        var patient = d.patient || {};

        if (filter === 'diseased' && !interp.diseased) return false;
        if (filter === 'normal' && interp.diseased) return false;

        if (query) {
          var stem = rec.stem.toLowerCase();
          var label = (interp.label || '').toLowerCase();
          var gender = (patient.gender || '').toLowerCase();
          if (stem.indexOf(query) === -1 && label.indexOf(query) === -1 && gender.indexOf(query) === -1) return false;
        }
        return true;
      });

      filtered.sort(function(a, b) {
        var va = getSortValue(a, sortField);
        var vb = getSortValue(b, sortField);
        if (va < vb) return sortDir === 'asc' ? -1 : 1;
        if (va > vb) return sortDir === 'asc' ? 1 : -1;
        return 0;
      });

      buildTable(filtered);
      updateSortHeaders();

      var countEl = document.getElementById('viewer-count');
      if (countEl) countEl.textContent = filtered.length + ' of ' + allRecords.length + ' records';
    }

    function initViewerControls() {
      var searchEl = document.getElementById('viewer-search');
      var filterEl = document.getElementById('viewer-filter');
      var sortEl = document.getElementById('viewer-sort');

      if (searchEl) searchEl.addEventListener('input', function() { applyFilters(); });
      if (filterEl) filterEl.addEventListener('change', function() { applyFilters(); });
      if (sortEl) sortEl.addEventListener('change', function() { sortDir = 'asc'; applyFilters(); });
    }

    function toggle(rec,idx,tr){
      var det=document.getElementById('det-'+idx);
      if(expandedIdx===idx){tr.classList.remove('expanded');det.classList.remove('visible');expandedIdx=-1;return;}
      if(expandedIdx>=0){
        var prev=tbody.querySelector('.data-row.expanded');if(prev)prev.classList.remove('expanded');
        var pd=document.getElementById('det-'+expandedIdx);if(pd)pd.classList.remove('visible');
      }
      expandedIdx=idx;tr.classList.add('expanded');det.classList.add('visible');

      var pre=det.querySelector('.detail-json pre');
      if(cache[rec.index]){pre.innerHTML=jsonTree(cache[rec.index],null,0,true);bindToggles(pre);}
      else{pre.textContent='Loading\u2026';fetch('/loki/api/record/'+rec.index).then(function(r){return r.json();}).then(function(d){cache[rec.index]=d;pre.innerHTML=jsonTree(d,null,0,true);bindToggles(pre);}).catch(function(){pre.textContent='Error';});}

      det.querySelector('.detail-image').onclick=function(e){e.stopPropagation();openLb(this.querySelector('img').src,rec.png_file);};
      var docEl = det.querySelector('.detail-doc');
      if (docEl) {
        docEl.onclick=function(e){
          e.stopPropagation();
          openDoc(this.dataset.doc, this.dataset.docName, this.dataset.docDl || '');
        };
      }
      var copyBtn = det.querySelector('.json-copy');
      if (copyBtn) {
        copyBtn.onclick = function(e) {
          e.stopPropagation();
          var data = cache[rec.index];
          if (!data) return;
          var text = JSON.stringify(data, null, 2);
          copyToClipboard(text, copyBtn);
        };
      }
    }

    function copyToClipboard(text, btn) {
      var done = function() {
        btn.classList.add('copied');
        var label = btn.querySelector('.json-copy-label');
        var prev = label ? label.textContent : '';
        if (label) label.textContent = 'Copied';
        setTimeout(function() {
          btn.classList.remove('copied');
          if (label) label.textContent = prev || 'Copy';
        }, 1500);
      };
      var fail = function() {
        var label = btn.querySelector('.json-copy-label');
        if (label) { var prev = label.textContent; label.textContent = 'Failed'; setTimeout(function(){ label.textContent = prev || 'Copy'; }, 1500); }
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(function(){ fallback(); });
      } else {
        fallback();
      }
      function fallback() {
        try {
          var ta = document.createElement('textarea');
          ta.value = text;
          ta.setAttribute('readonly','');
          ta.style.position='absolute'; ta.style.left='-9999px';
          document.body.appendChild(ta);
          ta.select();
          var ok = document.execCommand('copy');
          document.body.removeChild(ta);
          if (ok) done(); else fail();
        } catch (e) { fail(); }
      }
    }

    var docModal = document.getElementById('doc-modal');
    var docFrame = document.getElementById('doc-iframe');
    var docCaption = document.getElementById('doc-caption');
    var docClose = document.getElementById('doc-close');
    var docBackdrop = document.getElementById('doc-backdrop');
    var docDownload = document.getElementById('doc-download');
    var docOpen = false;
    function openDoc(url, name, dlUrl) {
      if (!docModal || !docFrame) return;
      docFrame.src = url;
      if (docCaption) docCaption.textContent = name || '';
      if (docDownload) {
        if (dlUrl) { docDownload.href = dlUrl; docDownload.style.display=''; }
        else { docDownload.removeAttribute('href'); docDownload.style.display='none'; }
      }
      docModal.classList.add('active');
      docModal.setAttribute('aria-hidden','false');
      document.body.style.overflow='hidden';
      setTimeout(function(){ docOpen = true; }, 60);
    }
    function closeDoc(e) {
      if (e) e.stopPropagation();
      if (!docOpen) return;
      docOpen = false;
      docModal.classList.remove('active');
      docModal.setAttribute('aria-hidden','true');
      document.body.style.overflow='';
      setTimeout(function(){ if (!docOpen && docFrame) docFrame.src='about:blank'; }, 250);
    }
    if (docClose) docClose.addEventListener('click', closeDoc);
    if (docBackdrop) docBackdrop.addEventListener('click', closeDoc);
    document.addEventListener('keydown', function(e){
      if (docOpen && e.key === 'Escape') closeDoc(e);
    });

    function applyLbTransform(){
      lightboxImg.style.transform='scale('+lbZoom+') translate('+lbPanX+'px,'+lbPanY+'px)';
      lightboxImg.style.cursor=lbZoom>1?'grab':'zoom-in';
      lbZoomLevel.textContent=Math.round(lbZoom*100)+'%';
    }

    function openLb(src,caption){
      lightboxImg.src=src;lightboxCaption.textContent=caption;
      lbZoom=1;lbPanX=0;lbPanY=0;applyLbTransform();
      lightbox.classList.add('active');lightbox.setAttribute('aria-hidden','false');
      document.body.style.overflow='hidden';
      setTimeout(function(){lbOpen=true;},60);
    }

    function closeLb(e){
      if(e)e.stopPropagation();if(!lbOpen)return;lbOpen=false;
      lightbox.classList.remove('active');lightbox.setAttribute('aria-hidden','true');
      document.body.style.overflow='';lbZoom=1;lbPanX=0;lbPanY=0;applyLbTransform();
    }

    lightboxClose.addEventListener('click',closeLb);
    lightboxBackdrop.addEventListener('click',closeLb);
    lbZoomIn.addEventListener('click',function(e){e.stopPropagation();lbZoom=Math.min(lbZoom+0.5,6);applyLbTransform();});
    lbZoomOut.addEventListener('click',function(e){e.stopPropagation();lbZoom=Math.max(lbZoom-0.5,0.5);if(lbZoom<=1){lbPanX=0;lbPanY=0;}applyLbTransform();});
    lbZoomReset.addEventListener('click',function(e){e.stopPropagation();lbZoom=1;lbPanX=0;lbPanY=0;applyLbTransform();});

    lightbox.addEventListener('wheel',function(e){if(!lbOpen)return;e.preventDefault();lbZoom=e.deltaY<0?Math.min(lbZoom+0.25,6):Math.max(lbZoom-0.25,0.5);if(lbZoom<=1){lbPanX=0;lbPanY=0;}applyLbTransform();},{passive:false});

    lightboxImg.addEventListener('dblclick',function(e){e.stopPropagation();if(lbZoom===1){lbZoom=3;}else{lbZoom=1;lbPanX=0;lbPanY=0;}applyLbTransform();});

    lightboxImg.addEventListener('mousedown',function(e){if(lbZoom<=1)return;panning=true;panStartX=e.clientX-lbPanX;panStartY=e.clientY-lbPanY;lightboxImg.style.cursor='grabbing';e.preventDefault();});
    document.addEventListener('mousemove',function(e){if(!panning)return;lbPanX=e.clientX-panStartX;lbPanY=e.clientY-panStartY;applyLbTransform();});
    document.addEventListener('mouseup',function(){if(panning){panning=false;lightboxImg.style.cursor=lbZoom>1?'grab':'zoom-in';}});

    document.addEventListener('keydown',function(e){
      if(!lbOpen)return;
      if(e.key==='Escape')closeLb(e);
      if(e.key==='='||e.key==='+'){lbZoom=Math.min(lbZoom+0.5,6);applyLbTransform();}
      if(e.key==='-'){lbZoom=Math.max(lbZoom-0.5,0.5);if(lbZoom<=1){lbPanX=0;lbPanY=0;}applyLbTransform();}
      if(e.key==='0'){lbZoom=1;lbPanX=0;lbPanY=0;applyLbTransform();}
    });

    fetch('/loki/api/records').then(function(r){return r.json();}).then(loadAllAndBuild).catch(function(){tbody.innerHTML='<tr class="loading-row"><td colspan="8">Failed to load.</td></tr>';});
  }
})();
