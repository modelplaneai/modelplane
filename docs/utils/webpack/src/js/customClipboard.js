// Customize clipboard.js
import * as ClipboardJS from 'clipboard';

/*
* Extends clipboard.js to support passing line numbers to copy instead of a
* whole code box.
*/

// copy API links to the clipboard
const crdURLCopier = new ClipboardJS('.kind-link');

// Most of the following is directly from the Bootstrap Website.
// https://github.com/twbs/bootstrap/blob/main/site/assets/js/code-examples.js
const btnHtml = [
  '   <div class="bd-clipboard">',
  '      <button type="button" class="btn-clipboard" title="Copy">',
  '        <svg class="bi" role="img" title="Copy" aria-label="Copy"><use xlink:href="#clipboard"/></svg>',
  '      </button>',
  '   </div>'
].join('')

// wrap programmatically code blocks and add copy btn.
var codeBlocks = document.querySelectorAll('.highlight')

for (var i = 0; i < codeBlocks.length; i++){
  var copyLines = getLines(codeBlocks[i])
  if(copyLines[0] == 0 && copyLines[1] == 0){
    continue
  }
  setHighlight(codeBlocks[i], copyLines)
  codeBlocks[i].insertAdjacentHTML('beforeend', btnHtml)
}

// use .parentNode.parentNode because the trigger is the <button> element.
// first parent is the .bd-clipboard div
// second parent is the .hightlight div
const clipboard = new ClipboardJS('.btn-clipboard', {
  target: trigger => trigger.parentNode.parentNode,
  text: trigger => getText(trigger.parentNode.parentNode)
  }
)

clipboard.on('success', event => {
  const iconFirstChild = event.trigger.querySelector('.bi').firstElementChild
  const namespace = 'http://www.w3.org/1999/xlink'
  const originalXhref = iconFirstChild.getAttributeNS(namespace, 'href')
  const originalTitle = event.trigger.title

  event.clearSelection()
  iconFirstChild.setAttributeNS(namespace, 'href', originalXhref.replace('clipboard', 'check2'))

  setTimeout(() => {
    iconFirstChild.setAttributeNS(namespace, 'href', originalXhref)
    event.trigger.title = originalTitle
  }, 2000)
})

clipboard.on('error', event => {
  const modifierKey = /mac/i.test(navigator.userAgent) ? '\u2318' : 'Ctrl-'
  const fallbackMsg = `Press ${modifierKey}C to copy`
})

// Get the lines from the code box based on the optional `copy-lines` attribute.
// Defaults to copying all lines.
function getText(highlightDiv){
  // Find the code lines inside the code table
  // ".line" contains the line number and text
  // .cl is just the text
  var codeLines = highlightDiv.getElementsByClassName("cl")
  var codeText = []

  // Get every line of code without markup
  for (var i = 0; i < codeLines.length; i++){
    codeText.push(codeLines[i].innerText)
  }

  var startEnd = getLines(highlightDiv)

  return codeText.slice((startEnd[0] - 1), startEnd[1]).join('')

}

// Parse the highlight div element and look for the "copy-lines" data attribute
// Return an array of [starting line number, ending line number]
function getLines(highlightDiv){

  var codeLinesLength = highlightDiv.getElementsByClassName("cl").length
  var startEnd = [1, codeLinesLength]

  // did they set the "copy-lines" value in the code box to identify what to copy?
  // if not, just return the first and last lines.
  if(!("copy-lines" in highlightDiv.attributes)){
    return startEnd
  }

  var copyVal = highlightDiv.attributes["copy-lines"].value
  var startLines = 1
  var endLines = codeLinesLength

  if(copyVal === "none"){
    return [0,0]
  }

  // if it's a single digit then start == end
  if(copyVal.length === 1){
    startLines = parseInt(copyVal, 10)
    endLines = parseInt(copyVal, 10)
  }
  // If the default copy is 1 instead of all, the following is required.
  // else if(copyVal.localeCompare("all") === 0){
  //     endLines = codeContent.length
  // }
  else {
    // look for a value like 1-10. Split on the dash.
    var startendLines = copyVal.split("-")
    startLines = parseInt(startendLines[0], 10)
    endLines = parseInt(startendLines[1], 10)
  }

  if(startLines <= 0 || isNaN(startLines)) {
    startLines = 1
  }
  if(endLines <= 0 || endLines > codeLinesLength - 1 || isNaN(endLines)){
    endLines = codeLinesLength
  }

  return [startLines, endLines]
}

// For each line that matches the defined "copy-lines" set class "copyHighlight"
// This makes finding these lines later much easier.
function setHighlight(highlightDiv, lineRange){
  var lines = highlightDiv.querySelectorAll('.line')
  var start = lineRange[0] - 1
  var end = lineRange[1] - 1

  for (var i = start ; i <= end; i++){
    lines[i].classList.add("copyHighlight")
  }
}

// For each "copyHighlight" line, toggle class "copyHover"
// This function is unique from setHighlight because it's called when the
// eventListener fires.
function copyHoverHighlight(clipboardElement){
  var codeblock = clipboardElement.parentNode
  var lines = codeblock.getElementsByClassName("copyHighlight")
  for (var j = 0 ; j < lines.length ; j++){
    lines[j].classList.toggle("copyHover")
  }
}

// Creates mouse over and mouse out event listeners on each clipboard element.
function build_eventlistener(){
  var clipboards = document.getElementsByClassName("bd-clipboard")
  for (var i = 0 ; i < clipboards.length ; i++){
    clipboards[i].addEventListener("mouseover", function(){ copyHoverHighlight(this) }, true)
    clipboards[i].addEventListener("mouseout", function(){ copyHoverHighlight(this) }, true)
  }

}

window.onload = build_eventlistener();
