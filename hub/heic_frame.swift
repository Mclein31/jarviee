// heic_frame <src> <index> <out.jpg>: writes one image of a multi-image HEIC (a dynamic macOS wallpaper keeps its
// light and dark appearances as separate images) as a JPEG, so Jarviee's dark glass can sit on the dark one.
import Foundation
import ImageIO
import UniformTypeIdentifiers

let a = CommandLine.arguments
guard a.count == 4, let idx = Int(a[2]),
      let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: a[1]) as CFURL, nil),
      idx >= 0, idx < CGImageSourceGetCount(src),
      let img = CGImageSourceCreateImageAtIndex(src, idx, nil),
      let dst = CGImageDestinationCreateWithURL(URL(fileURLWithPath: a[3]) as CFURL, UTType.jpeg.identifier as CFString, 1, nil)
else { exit(2) }
CGImageDestinationAddImage(dst, img, [kCGImageDestinationLossyCompressionQuality: 0.86] as CFDictionary)
exit(CGImageDestinationFinalize(dst) ? 0 : 1)
