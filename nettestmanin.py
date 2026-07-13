from app.utils.text_utils import remove_html_tags

teststr = '<div id="yuanbao-options" aria-label="yuanbao-options" style="width: 0px; height: 0px; position: absolute; top: 0px; left: 0px; overflow: hidden"><div id="yuanbao-option-text-selection" aria-label="yuanbao-option-text-selection" aria-description="" class=""></div></div>'

teststr = remove_html_tags(teststr)
