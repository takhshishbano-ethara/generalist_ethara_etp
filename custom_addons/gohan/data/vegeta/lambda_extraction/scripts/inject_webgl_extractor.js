(() => {
  const result = {
    detected: false,
    canvases: [],
    three_js: null,
    shaders: [],
    webgl_info: null,
  };

  const canvases = document.querySelectorAll('canvas');
  if (canvases.length === 0) return result;

  canvases.forEach((canvas, i) => {
    const rect = canvas.getBoundingClientRect();
    const info = {
      index: i,
      width: canvas.width,
      height: canvas.height,
      cssWidth: Math.round(rect.width),
      cssHeight: Math.round(rect.height),
      pixelRatio: canvas.width / (rect.width || 1),
      classes: Array.from(canvas.classList),
      id: canvas.id || null,
      dataAttributes: {},
    };

    for (const attr of canvas.attributes) {
      if (attr.name.startsWith('data-')) {
        info.dataAttributes[attr.name] = attr.value;
      }
    }

    let gl = null;
    try { gl = canvas.getContext('webgl2'); } catch (e) {}
    if (!gl) try { gl = canvas.getContext('webgl'); } catch (e) {}
    if (!gl) try { gl = canvas.getContext('experimental-webgl'); } catch (e) {}

    if (gl) {
      result.detected = true;
      info.contextType = gl.constructor.name === 'WebGL2RenderingContext' ? 'webgl2' : 'webgl';

      const debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
      if (debugInfo) {
        info.gpuVendor = gl.getParameter(debugInfo.UNMASKED_VENDOR_WEBGL);
        info.gpuRenderer = gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL);
      }

      info.maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE);
      info.maxViewportDims = Array.from(gl.getParameter(gl.MAX_VIEWPORT_DIMS));
      info.antialias = gl.getContextAttributes()?.antialias || false;
      info.alpha = gl.getContextAttributes()?.alpha || false;
      info.stencil = gl.getContextAttributes()?.stencil || false;
      info.preserveDrawingBuffer = gl.getContextAttributes()?.preserveDrawingBuffer || false;

      if (!result.webgl_info) {
        result.webgl_info = {
          contextType: info.contextType,
          gpuVendor: info.gpuVendor,
          gpuRenderer: info.gpuRenderer,
          maxTextureSize: info.maxTextureSize,
          antialias: info.antialias,
        };
      }
    }

    result.canvases.push(info);
  });

  // --- THREE.JS DETECTION (ESM-safe) ---
  // Strategy 1: Parse data-engine attribute (Three.js r150+ stamps this)
  let threeVersion = null;
  let threeRendererType = null;
  let dataEngineStr = null;
  const engineCanvas = document.querySelector('canvas[data-engine]');
  if (engineCanvas) {
    dataEngineStr = engineCanvas.dataset.engine;
    const versionMatch = dataEngineStr.match(/three\.js\s+r(\d+)/i);
    if (versionMatch) {
      threeVersion = versionMatch[1];
    }
    if (dataEngineStr.toLowerCase().includes('webgpu')) {
      threeRendererType = 'WebGPURenderer';
    } else {
      threeRendererType = 'WebGLRenderer';
    }
  }

  // Strategy 2: Traditional window.THREE check
  const hasWindowTHREE = typeof window.THREE !== 'undefined' && window.THREE;
  if (!threeVersion && hasWindowTHREE) {
    threeVersion = window.THREE.REVISION || 'unknown';
  }

  // Strategy 3: Check __r3f store (React Three Fiber)
  let r3fDetected = false;
  try {
    const roots = document.querySelectorAll('canvas');
    roots.forEach(c => {
      if (c.__r3f) r3fDetected = true;
    });
  } catch (e) {}
  if (document.querySelector('[data-reactroot]') && (threeVersion || hasWindowTHREE)) {
    r3fDetected = true;
  }

  // Strategy 4: Check performance entries for loaded 3D assets
  const perfEntries = performance.getEntriesByType('resource') || [];
  const detected3DAssets = {};
  const assetPatterns = {
    gltf: /\.(gltf|glb)\b/i,
    draco: /draco/i,
    hdr: /\.(hdr|exr)\b/i,
    ktx2: /\.ktx2\b/i,
    fbx: /\.fbx\b/i,
    obj: /\.(obj|mtl)\b/i,
    basis: /\.basis\b/i,
  };

  for (const entry of perfEntries) {
    const url = entry.name;
    for (const [type, pattern] of Object.entries(assetPatterns)) {
      if (pattern.test(url)) {
        if (!detected3DAssets[type]) detected3DAssets[type] = [];
        if (!detected3DAssets[type].includes(url)) {
          detected3DAssets[type].push(url);
        }
      }
    }
  }

  // Also check DOM for 3D asset references
  const allLinks = Array.from(document.querySelectorAll('[href], [src], [data-src]'));
  for (const el of allLinks) {
    const url = el.href || el.src || el.dataset?.src || '';
    for (const [type, pattern] of Object.entries(assetPatterns)) {
      if (pattern.test(url)) {
        if (!detected3DAssets[type]) detected3DAssets[type] = [];
        if (!detected3DAssets[type].includes(url)) {
          detected3DAssets[type].push(url);
        }
      }
    }
  }

  // Build Three.js info if detected via ANY strategy
  if (threeVersion || hasWindowTHREE) {
    const threeInfo = {
      revision: threeVersion || 'unknown',
      renderer: null,
      controls: null,
      physics: null,
      postProcessing: [],
      loaders: [],
      codeHints: [],
    };

    // Renderer info
    if (threeRendererType) {
      threeInfo.renderer = {
        type: threeRendererType,
        dataEngine: dataEngineStr,
      };
    }

    // If window.THREE exists, do deep inspection
    if (hasWindowTHREE) {
      // Try finding renderer instance
      for (const key of Object.keys(window)) {
        try {
          const obj = window[key];
          if (obj && obj.constructor && obj.constructor.name === 'WebGLRenderer') {
            threeInfo.renderer = {
              type: 'WebGLRenderer',
              toneMapping: obj.toneMapping,
              toneMappingExposure: obj.toneMappingExposure,
              outputColorSpace: obj.outputColorSpace || obj.outputEncoding,
              shadowMapEnabled: obj.shadowMap?.enabled || false,
              shadowMapType: obj.shadowMap?.type,
              pixelRatio: obj.getPixelRatio ? obj.getPixelRatio() : null,
              antialias: obj.domElement?.getContext('webgl2')?.getContextAttributes()?.antialias,
            };
            break;
          }
        } catch (e) {}
      }

      // Feature checks via THREE namespace
      const featureChecks = {
        postProcessing: [
          'EffectComposer', 'RenderPass', 'UnrealBloomPass', 'SSAOPass',
          'SMAAPass', 'FXAAShader', 'BokehPass', 'FilmPass', 'GlitchPass',
          'OutlinePass', 'ShaderPass',
        ],
        controls: [
          'OrbitControls', 'FlyControls', 'FirstPersonControls',
          'PointerLockControls', 'TrackballControls', 'DragControls',
        ],
        loaders: [
          'GLTFLoader', 'DRACOLoader', 'FBXLoader', 'OBJLoader',
          'TextureLoader', 'CubeTextureLoader', 'RGBELoader',
          'KTX2Loader', 'EXRLoader',
        ],
        physics: ['Ammo', 'CANNON', 'Rapier', 'oimo'],
      };

      const scriptSrcs = Array.from(document.querySelectorAll('script[src]')).map(s => s.src.toLowerCase());

      for (const [category, names] of Object.entries(featureChecks)) {
        for (const name of names) {
          if (window[name] || (window.THREE && window.THREE[name])) {
            if (category === 'postProcessing') threeInfo.postProcessing.push(name);
            else if (category === 'controls') threeInfo.controls = name;
            else if (category === 'loaders') threeInfo.loaders.push(name);
          }
          const nameLower = name.toLowerCase();
          if (scriptSrcs.some(s => s.includes(nameLower))) {
            if (category === 'postProcessing' && !threeInfo.postProcessing.includes(name)) {
              threeInfo.postProcessing.push(name);
            }
            if (category === 'loaders' && !threeInfo.loaders.includes(name)) {
              threeInfo.loaders.push(name);
            }
          }
        }
      }

      for (const engine of featureChecks.physics) {
        if (window[engine]) {
          threeInfo.physics = engine;
          break;
        }
      }
    }

    // Code hint scanning (works for both ESM and global builds)
    const codeHints = new Set();
    const patterns = [
      [/PerspectiveCamera/i, 'PerspectiveCamera'],
      [/OrthographicCamera/i, 'OrthographicCamera'],
      [/PointLight|DirectionalLight|SpotLight|AmbientLight|HemisphereLight|RectAreaLight/i, 'Lighting'],
      [/MeshStandardMaterial|MeshPhysicalMaterial|MeshBasicMaterial|ShaderMaterial|MeshNormalMaterial|MeshLambertMaterial|MeshPhongMaterial|MeshToonMaterial/i, 'Materials'],
      [/InstancedMesh/i, 'InstancedMesh'],
      [/Raycaster/i, 'Raycaster'],
      [/GLTFLoader|gltf/i, 'GLTFLoader'],
      [/DRACOLoader|draco/i, 'DRACOLoader'],
      [/requestAnimationFrame/i, 'AnimationLoop'],
      [/EffectComposer|postprocessing/i, 'PostProcessing'],
      [/BufferGeometry/i, 'BufferGeometry'],
      [/ShaderMaterial|RawShaderMaterial|vertexShader|fragmentShader/i, 'CustomShaders'],
      [/Texture|TextureLoader/i, 'Textures'],
      [/CubeTexture|EnvironmentMap|envMap/i, 'EnvironmentMapping'],
      [/Shadow|shadowMap|castShadow|receiveShadow/i, 'Shadows'],
      [/Fog|FogExp2/i, 'Fog'],
      [/AnimationMixer|AnimationClip/i, 'SkeletalAnimation'],
      [/Cannon|Ammo|Rapier|oimo|physic/i, 'Physics'],
      [/Audio|AudioListener|PositionalAudio/i, 'Audio3D'],
      [/CSS2DRenderer|CSS3DRenderer/i, 'CSSOverlay'],
      [/dat\.gui|lil-gui|tweakpane/i, 'DebugGUI'],
      [/OrbitControls/i, 'OrbitControls'],
      [/PointerLockControls/i, 'PointerLockControls'],
      [/FirstPersonControls/i, 'FirstPersonControls'],
    ];

    // Scan inline scripts
    const inlineScripts = document.querySelectorAll('script:not([src])');
    let scannedCount = 0;
    for (const script of inlineScripts) {
      if (scannedCount >= 5) break;
      const text = script.textContent || '';
      if (text.length < 100) continue;
      scannedCount++;
      for (const [regex, label] of patterns) {
        if (regex.test(text)) codeHints.add(label);
      }
    }

    // Scan module scripts too
    const moduleScripts = document.querySelectorAll('script[type="module"]:not([src])');
    for (const script of moduleScripts) {
      const text = script.textContent || '';
      if (text.length < 50) continue;
      for (const [regex, label] of patterns) {
        if (regex.test(text)) codeHints.add(label);
      }
    }

    // Check script URLs for hints
    const scriptSrcs = Array.from(document.querySelectorAll('script[src]')).map(s => s.src.toLowerCase());
    for (const src of scriptSrcs) {
      for (const [regex, label] of patterns) {
        if (regex.test(src)) codeHints.add(label);
      }
    }

    // Infer from detected assets
    if (detected3DAssets.gltf?.length) codeHints.add('GLTFLoader');
    if (detected3DAssets.draco?.length) codeHints.add('DRACOLoader');
    if (detected3DAssets.hdr?.length) codeHints.add('EnvironmentMapping');
    if (detected3DAssets.ktx2?.length) codeHints.add('KTX2Loader');

    // Infer from performance entries (loaded script names)
    for (const entry of perfEntries) {
      const url = entry.name.toLowerCase();
      if (url.includes('cannon') || url.includes('rapier') || url.includes('ammo') || url.includes('oimo')) {
        codeHints.add('Physics');
        if (!threeInfo.physics) {
          if (url.includes('cannon')) threeInfo.physics = 'cannon-es';
          else if (url.includes('rapier')) threeInfo.physics = 'Rapier';
          else if (url.includes('ammo')) threeInfo.physics = 'Ammo';
          else if (url.includes('oimo')) threeInfo.physics = 'Oimo';
        }
      }
      if (url.includes('postprocessing') || url.includes('effectcomposer')) codeHints.add('PostProcessing');
      if (url.includes('orbit') && url.includes('control')) codeHints.add('OrbitControls');
    }

    threeInfo.codeHints = Array.from(codeHints).sort();
    result.three_js = threeInfo;
  }

  result.r3f_likely = r3fDetected;
  result.detected3DAssets = detected3DAssets;

  // Detect shader sources
  try {
    const shaderScripts = document.querySelectorAll('script[type="x-shader/x-vertex"], script[type="x-shader/x-fragment"], script[type="glsl"]');
    for (const shader of shaderScripts) {
      result.shaders.push({
        type: shader.type,
        id: shader.id || null,
        length: (shader.textContent || '').length,
        preview: (shader.textContent || '').substring(0, 200).trim(),
      });
    }
  } catch (e) {}

  return result;
})();
