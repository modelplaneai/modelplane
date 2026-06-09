/*
*    Create the hover to highlight function.
*
*    Finds the code box matching the label and updates the code box to add a
*    highlight function.
*/

export function getKeywords(){
    var keywordList = document.getElementsByTagName("highlight-term")

    for (let i = 0; i < keywordList.length; i++) {
        build_eventlistener(keywordList[i].id, keywordList[i].dataset.label, keywordList[i].dataset.line)
    }
}


export function build_eventlistener(command_id, code_box_label, code_line_number){

    // the <mouseover> elment of the individual command calling the shortcode
    var command = document.getElementById(command_id);

    // the outer div for the fenced code block
    var code_box = document.querySelector("div[label=" + code_box_label + "]");

    // the element of the source code text we want
    var code_line_text = code_box.getElementsByClassName("cl")[code_line_number - 1 ];

    command.addEventListener('mouseover', function handleMouseOver() {
        code_line_text.classList.toggle("hl");
    });

    command.addEventListener('mouseout', function handleMouseOut() {
        code_line_text.classList.toggle("hl");
    });

}

window.onload = getKeywords();