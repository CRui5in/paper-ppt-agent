/** Keep each imported paragraph's line box on the same type scale as its runs. */
export const normalizeParagraphFontSizes = (html: string): string => {
  const doc = new DOMParser().parseFromString(html, 'text/html')
  for (const paragraph of doc.querySelectorAll<HTMLElement>('p, li')) {
    const styledElements = [paragraph, ...paragraph.querySelectorAll<HTMLElement>('[style]')]
    const sizes = styledElements.flatMap(element => {
      const match = element.style.fontSize.match(/^([\d.]+)px$/)
      return match ? [Number(match[1])] : []
    }).filter(size => size > 0)
    if (sizes.length) paragraph.style.fontSize = `${Math.max(...sizes)}px`
  }
  return doc.body.innerHTML
}
