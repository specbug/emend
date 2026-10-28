# When Calibre and EPUBCheck disagree

Calibre’s **Edit book → Tools → Check book** is useful, but it is not EPUBCheck.
It checks XML parsing, links, images, styles and other common issues without
enforcing every EPUB schema rule. “No problems found” in Calibre does not establish
that the EPUB passes EPUBCheck. Emend requires EPUBCheck by default.

First make sure you are comparing the same version of the book. Emend keeps its
own preserved copy; editing the Calibre library file does not update that copy.
**Run everything again** checks Emend’s current file, not a newly edited Calibre file.

Emend tries its supported repairs by itself after import and on **Run everything
again**; the book's **Repair** step lists what it applied, with a link to the exact
change. If **Fix HTML markup** was applied, you do not need to edit or reimport a copy
manually. Emend repairs empty EPUB 2 root language attributes from one
unambiguous package language and block-only paragraph wrappers inside `body` or
`div`. Nonempty language values, mixed prose and uncertain nesting remain manual.

## Invalid language attributes

Open the file named in the error in Calibre’s editor and inspect the reported
line. For example, an English document might begin:

```xml
<html xmlns="http://www.w3.org/1999/xhtml" lang="en" xml:lang="en">
```

Use a valid tag for the actual language: `en` for English, `en-US` when US English
is specifically intended. Use the registered language tag rather than the display
name `English`; use hyphens rather than underscores such as `en_US`. Inspect both `lang` and `xml:lang`
when present, including overrides inside the document. Preserve intentional
language differences in quotations and other passages. Changing Calibre’s library
language metadata alone does not repair attributes inside the EPUB’s HTML.

## Heading or list “not allowed here”

The heading or list may be valid by itself but invalid inside its parent. For
example, EPUB 2 does not permit this nesting:

```xml
<p><h1>Contents</h1><ul><li>Chapter one</li></ul></p>
```

One possible correction, when the paragraph contains only those blocks, is:

```xml
<div><h1>Contents</h1><ul><li>Chapter one</li></ul></div>
```

This is an example, not a universal replacement. Inspect the complete opening and
closing tags around the reported lines. The offending container could instead be
an inline element, and a paragraph containing prose may need splitting. Preserve
all text, IDs, links and relevant attributes. A changed element can affect CSS,
so preview the affected pages afterwards. Do not replace all paragraphs with divs.

## Recheck the edited copy

1. Save the edits in Calibre’s editor.
2. Export the edited EPUB using Calibre’s **Save to disk**, outside the active
   Calibre library. Import that EPUB into Emend.
3. Check the new result, then preview the affected pages before sending it.

Saving or reimporting an unchanged file will not fix schema errors. Beyond the
scoped markup repairs above, Emend supports packaging and selected contents
links; arbitrary HTML nesting and language attributes still need manual editing.
Failed required checks continue to block download and publication.
