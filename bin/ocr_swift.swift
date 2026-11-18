import Foundation
import Vision
import AppKit

// 本机 OCR: macOS Vision 框架, 仅用于识别用户刚上传的题目照片 (不扫描历史 PDF)
guard CommandLine.arguments.count >= 2 else {
    FileHandle.standardError.write("usage: ocr <image-path>\n".data(using: .utf8)!)
    exit(2)
}
let path = CommandLine.arguments[1]
guard let img = NSImage(contentsOfFile: path),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot read image\n".data(using: .utf8)!)
    exit(3)
}
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = false
req.recognitionLanguages = ["en-US"]
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
try handler.perform([req])
var lines: [String] = []
for obs in (req.results ?? []) {
    if let t = obs.topCandidates(1).first?.string.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty {
        lines.append(t)
    }
}
print(lines.joined(separator: "\n"))
