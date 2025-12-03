// Simple TinyMCE initialisation for policy content fields in Django admin.
// This script assumes TinyMCE is already loaded from the CDN.

document.addEventListener("DOMContentLoaded", function () {
  if (typeof tinymce === "undefined") {
    return;
  }

  tinymce.init({
    selector: "textarea.tinymce-policy",
    // Show full menubar so all options are clearly visible
    menubar: "file edit view insert format tools table help",
    plugins:
      "advlist autolink lists link charmap searchreplace visualblocks visualchars fullscreen table code wordcount",
    toolbar:
      "undo redo | blocks | bold italic underline | alignleft aligncenter alignright alignjustify | bullist numlist outdent indent | link table | removeformat | fullscreen code",
    toolbar_mode: "sliding",
    height: 420,
    branding: false,
    convert_urls: false,
  });
});
