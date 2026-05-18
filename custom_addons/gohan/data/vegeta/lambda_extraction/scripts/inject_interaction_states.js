((selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;

  const captureStyles = (element) => {
    const s = getComputedStyle(element);
    return {
      color: s.color,
      backgroundColor: s.backgroundColor,
      borderColor: s.borderColor,
      boxShadow: s.boxShadow,
      transform: s.transform,
      opacity: s.opacity,
      scale: s.scale,
      filter: s.filter,
      textDecoration: s.textDecoration,
      outline: s.outline,
      outlineColor: s.outlineColor,
      outlineOffset: s.outlineOffset,
      cursor: s.cursor,
      transition: s.transition,
      transitionDuration: s.transitionDuration,
      transitionTimingFunction: s.transitionTimingFunction,
      transitionProperty: s.transitionProperty,
      width: s.width,
      height: s.height,
      padding: s.padding,
      borderRadius: s.borderRadius,
      letterSpacing: s.letterSpacing,
      textTransform: s.textTransform,
      fontWeight: s.fontWeight,
      fontSize: s.fontSize,
      backgroundImage: s.backgroundImage,
      clipPath: s.clipPath,
      backdropFilter: s.backdropFilter || s.webkitBackdropFilter,
      mixBlendMode: s.mixBlendMode,
      borderWidth: s.borderWidth,
      borderStyle: s.borderStyle,
      gap: s.gap,
      translate: s.translate,
      rotate: s.rotate,
    };
  };

  return {
    selector,
    tag: el.tagName.toLowerCase(),
    text: el.textContent?.substring(0, 50)?.trim(),
    id: el.id || null,
    classes: Array.from(el.classList).slice(0, 5),
    rect: el.getBoundingClientRect().toJSON(),
    default_state: captureStyles(el),
  };
})
