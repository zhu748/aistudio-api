const owners = new Set<string>()
let previousOverflow = ''

export function setModalScroll(id: string, open: boolean) {
  if (open) {
    if (!owners.size) previousOverflow = document.body.style.overflow
    owners.add(id)
  } else {
    owners.delete(id)
  }
  document.body.style.overflow = owners.size ? 'hidden' : previousOverflow
}
