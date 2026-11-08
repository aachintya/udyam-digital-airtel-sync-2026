import Foundation
import Vision
import AppKit
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.recognitionLanguages = ["en-US"]
func baseline(_ r: VNRecognizedTextObservation) -> Double {
 let leftY = (r.topLeft.y + r.bottomLeft.y) / 2
 let rightY = (r.topRight.y + r.bottomRight.y) / 2
 let dx = max(0.001, (r.topRight.x + r.bottomRight.x - r.topLeft.x - r.bottomLeft.x) / 2)
 let slope = (rightY-leftY) / dx
 return r.boundingBox.midY - slope * (r.boundingBox.midX - 0.1)
}
do {
 guard let image = NSImage(contentsOf: url), let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {exit(1)}
 let width = cg.width, height = cg.height
 let pixels = UnsafeMutablePointer<UInt8>.allocate(capacity: width*height*4)
 defer { pixels.deallocate() }
 guard let context = CGContext(data: pixels, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width*4, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {exit(1)}
 context.setFillColor(NSColor.white.cgColor); context.fill(CGRect(x:0,y:0,width:width,height:height))
 context.draw(cg, in: CGRect(x:0,y:0,width:width,height:height))
 for i in stride(from:0,to:width*height*4,by:4) {
 let dark = max(pixels[i],max(pixels[i+1],pixels[i+2])) < 170
 pixels[i] = dark ? 0:255; pixels[i+1] = pixels[i];pixels[i+2]=pixels[i];pixels[i+3]=255
 }
 guard let cleaned = context.makeImage() else {exit(1)}
 try VNImageRequestHandler(cgImage: cleaned).perform([request])
 let lines = (request.results ?? []).sorted { a,b in abs(baseline(a)-baseline(b))>0.012 ? baseline(a)>baseline(b) : a.boundingBox.minX<b.boundingBox.minX }.compactMap { r -> [String:Any]? in
 guard let c = r.topCandidates(1).first else {return nil};return ["text":c.string,"confidence":c.confidence,"x":r.boundingBox.minX,"y":baseline(r)]
 }
 let data = try JSONSerialization.data(withJSONObject: ["lines":lines], options: [.sortedKeys])
 print(String(data:data,encoding:.utf8)!)
} catch { fputs("OCR could not read this image\n",stderr);exit(1) }
